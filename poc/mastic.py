'''The Mastic VDAF'''

import itertools
from typing import Optional, TypeAlias, TypeVar, cast

from vdaf_poc.common import (byte, concat, front, to_be_bytes, to_le_bytes,
                             vec_add, vec_sub, zeros)
from vdaf_poc.field import Field64, Field128, NttField
from vdaf_poc.flp_bbcggi19 import (Count, FlpBBCGGI19, Histogram,
                                   MultihotCountVec, Sum, SumVec, Valid)
from vdaf_poc.vdaf import Vdaf
from vdaf_poc.xof import XofTurboShake128

from dst import (USAGE_JOINT_RAND, USAGE_JOINT_RAND_PART,
                 USAGE_JOINT_RAND_SEED, USAGE_PROOF_SHARE, USAGE_PROVE_RAND,
                 USAGE_QUERY_RAND, dst)
from vidpf import CorrectionWord, Vidpf

W = TypeVar("W")
R = TypeVar("R")
F = TypeVar("F", bound=NttField)

MasticAggParam: TypeAlias = tuple[
    int,                            # level
    tuple[tuple[bool, ...], ...],   # prefixes
    bool,                           # whether to do the weight check
]

MasticPublicShare: TypeAlias = tuple[
    list[CorrectionWord],   # VIDPF correction words
    Optional[list[bytes]],  # FLP joint randomness parts
]

MasticInputShare: TypeAlias = tuple[
    bytes,              # VIDPF key
    Optional[list[F]],  # FLP leader proof share
    Optional[bytes],    # FLP seed
    list[F],            # beta share
]

MasticPrepState: TypeAlias = tuple[
    list[F],          # Truncated output share
    Optional[bytes],  # Predicted FLP joint rand seed
]

MasticPrepShare: TypeAlias = tuple[
    bytes,              # VIDPF proof
    Optional[list[F]],  # FLP verifier share
    Optional[bytes],    # FLP joint randomness part
]

MasticPrepMessage: TypeAlias = Optional[bytes]  # FLP joint rand seed


class Mastic(
        Vdaf[
            tuple[tuple[bool, ...], W],  # W
            MasticAggParam,
            MasticPublicShare,
            MasticInputShare,
            list[F],  # OutShare
            list[F],  # AggShare
            list[R],  # R
            MasticPrepState,
            MasticPrepShare,
            MasticPrepMessage,
        ]):

    # NOTE We'd like to make this generic, but this appears to be blocked
    # by a bug. We would add `Generic[W, R, X, F]` as
    # one of the super classes of `Mastic`, but this causes a runtime
    # error.
    xof = XofTurboShake128

    ID: int = 0xFFFFFFFF
    VERIFY_KEY_SIZE = XofTurboShake128.SEED_SIZE
    NONCE_SIZE = 16
    SHARES = 2
    ROUNDS = 1

    # Name of the VDAF, for use in test vector filenames.
    test_vec_name = 'Mastic'

    def __init__(self,
                 bits: int,
                 valid: Valid[W, R, F]):
        self.field = valid.field
        self.flp = FlpBBCGGI19(valid)
        # VIDPF is instantiated with the truncated beta, so the length is
        # OUTPUT_LEN instead of MEAS_LEN
        self.vidpf = Vidpf(valid.field, bits, valid.OUTPUT_LEN)
        self.RAND_SIZE = self.vidpf.RAND_SIZE + 2 * self.xof.SEED_SIZE + self.flp.MEAS_LEN
        if self.flp.JOINT_RAND_LEN > 0:  # FLP leader seed
            self.RAND_SIZE += self.xof.SEED_SIZE

    def shard(self,
              ctx: bytes,
              measurement: tuple[tuple[bool, ...], W],
              nonce: bytes,
              rand: bytes,
              ) -> tuple[MasticPublicShare, list[MasticInputShare]]:
        if self.flp.JOINT_RAND_LEN > 0:
            return self.shard_with_joint_rand(
                ctx, measurement, nonce, rand)
        return self.shard_without_joint_rand(
            ctx, measurement, nonce, rand)

    def shard_without_joint_rand(
            self,
            ctx: bytes,
            measurement: tuple[tuple[bool, ...], W],
            nonce: bytes,
            rand: bytes,
    ) -> tuple[MasticPublicShare, list[MasticInputShare]]:
        (vidpf_rand, rand) = front(self.vidpf.RAND_SIZE, rand)
        (prove_rand_seed, rand) = front(self.xof.SEED_SIZE, rand)
        (helper_seed, rand) = front(self.xof.SEED_SIZE, rand)
        (beta_helper_seed, rand) = front(self.xof.SEED_SIZE, rand)
        assert len(rand) == 0  # REMOVE ME

        (alpha, weight) = measurement
        beta = self.flp.encode(weight)
        beta_truncated = self.flp.truncate(beta)

        # Generate VIDPF keys.
        (correction_words, keys) = \
            self.vidpf.gen(alpha, beta_truncated, ctx, nonce, vidpf_rand)

        # Generate FLP and split it into shares.
        prove_rand = self.prove_rand(ctx, prove_rand_seed)
        proof = self.flp.prove(beta, prove_rand, [])
        helper_proof_share = self.helper_proof_share(ctx, helper_seed)
        leader_proof_share = vec_sub(proof, helper_proof_share)

        # TODO: here
        # beta_helper_share = [self.field(1)] * self.flp.MEAS_LEN
        # self.flp.MEAS_LEN
        beta_helper_share = self.beta_share_from_seed(ctx, beta_helper_seed)
        beta_leader_share = vec_sub(beta, beta_helper_share)

        public_share = (correction_words, None)
        input_shares = [
            (keys[0], leader_proof_share, None, beta_leader_share),
            (keys[1], None, helper_seed, beta_helper_share),
        ]
        return (public_share, input_shares)

    def shard_with_joint_rand(
            self,
            ctx: bytes,
            measurement: tuple[tuple[bool, ...], W],
            nonce: bytes,
            rand: bytes,
    ) -> tuple[MasticPublicShare, list[MasticInputShare]]:
        (vidpf_rand, rand) = front(self.vidpf.RAND_SIZE, rand)
        (prove_rand_seed, rand) = front(self.xof.SEED_SIZE, rand)
        (leader_seed, rand) = front(self.xof.SEED_SIZE, rand)
        (helper_seed, rand) = front(self.xof.SEED_SIZE, rand)
        (beta_helper_seed, rand) = front(self.xof.SEED_SIZE, rand)
        assert len(rand) == 0  # REMOVE ME

        (alpha, weight) = measurement
        beta = self.flp.encode(weight)
        beta_truncated = self.flp.truncate(beta)

        # Generate VIDPF keys.
        (correction_words, keys) = \
            self.vidpf.gen(alpha, beta_truncated, ctx, nonce, vidpf_rand)

        # Generate FLP joint randomness.
        joint_rand_parts = [
            self.joint_rand_part(
                ctx, 0, leader_seed, keys[0], correction_words, nonce),
            self.joint_rand_part(
                ctx, 1, helper_seed, keys[1], correction_words, nonce),
        ]
        joint_rand = self.joint_rand(
            ctx, self.joint_rand_seed(ctx, joint_rand_parts))

        # Generate FLP and split it into shares.
        prove_rand = self.prove_rand(ctx, prove_rand_seed)
        proof = self.flp.prove(beta, prove_rand, joint_rand)
        helper_proof_share = self.helper_proof_share(ctx, helper_seed)
        leader_proof_share = vec_sub(proof, helper_proof_share)

        # TODO:
        beta_helper_share = self.beta_share_from_seed(ctx, beta_helper_seed)
        beta_leader_share = vec_sub(beta, beta_helper_share)

        public_share = (correction_words, joint_rand_parts)
        input_shares = [
            (keys[0], leader_proof_share, leader_seed, beta_leader_share),
            (keys[1], None, cast(Optional[bytes], helper_seed), beta_helper_share),
        ]
        return (public_share, input_shares)

    def is_valid(self,
                 agg_param: MasticAggParam,
                 previous_agg_params: list[MasticAggParam],
                 ) -> bool:
        (level, _prefixes, do_weight_check) = agg_param

        # Check that the weight check is done exactly once.
        weight_checked = \
            (do_weight_check and len(previous_agg_params) == 0) or \
            (not do_weight_check and
                any(agg_param[2] for agg_param in previous_agg_params))

        # Check that the level is strictly increasing.
        level_increased = len(previous_agg_params) == 0 or \
            level > previous_agg_params[-1][0]

        return weight_checked and level_increased

    def prep_init(
            self,
            verify_key: bytes,
            ctx: bytes,
            agg_id: int,
            agg_param: MasticAggParam,
            nonce: bytes,
            public_share: MasticPublicShare,
            input_share: MasticInputShare,
    ) -> tuple[MasticPrepState, MasticPrepShare]:
        (level, prefixes, do_weight_check) = agg_param
        (key, proof_share, seed, beta_share) = \
            self.expand_input_share(ctx, agg_id, input_share)
        (correction_words, joint_rand_parts) = public_share

        # Evaluate the VIDPF.
        (truncated_beta_share, out_share, eval_proof) = self.vidpf.eval(
            agg_id,
            correction_words,
            key,
            level,
            prefixes,
            ctx,
            nonce,
        )

        # Query the FLP if applicable.
        joint_rand_part = None
        joint_rand_seed = None
        verifier_share = None
        if do_weight_check:
            query_rand = self.query_rand(verify_key, ctx, nonce, level)
            joint_rand = []
            if self.flp.JOINT_RAND_LEN > 0:
                assert seed is not None
                assert joint_rand_parts is not None
                joint_rand_part = self.joint_rand_part(
                    ctx, agg_id, seed, key, correction_words, nonce)
                joint_rand_parts[agg_id] = joint_rand_part
                joint_rand_seed = self.joint_rand_seed(
                    ctx, joint_rand_parts)
                joint_rand = self.joint_rand(
                    ctx, self.joint_rand_seed(ctx, joint_rand_parts))
            verifier_share = self.flp.query(
                beta_share,
                proof_share,
                query_rand,
                joint_rand,
                2,
            )

        # TODO: check that truncate(beta_share) is truncated_beta_share

        # Concatenate the output shares into one aggregatable output,
        # applying the FLP truncation algorithm on each FLP measurement
        # share.
        truncated_out_share = []
        for val_share in out_share:
            truncated_out_share += [val_share[0]] + \
                self.flp.truncate(val_share[1:])

        prep_state = (truncated_out_share, joint_rand_seed)
        prep_share = (eval_proof, verifier_share, joint_rand_part)
        return (prep_state, prep_share)

    def prep_shares_to_prep(
            self,
            ctx: bytes,
            agg_param: MasticAggParam,
            prep_shares: list[MasticPrepShare],
    ) -> MasticPrepMessage:
        (_level, _prefixes, do_weight_check) = agg_param

        if len(prep_shares) != 2:
            raise ValueError('unexpected number of prep shares')

        # assert self.flp.truncate(beta_share) == truncated_beta_share


        (eval_proof_0,
         verifier_share_0,
         joint_rand_part_0) = prep_shares[0]
        (eval_proof_1,
         verifier_share_1,
         joint_rand_part_1) = prep_shares[1]

        # Verify the VIDPF output.
        if eval_proof_0 != eval_proof_1:
            raise Exception('VIDPF verification failed')

        if not do_weight_check:
            return None
        if verifier_share_0 is None or verifier_share_1 is None:
            raise ValueError('expected FLP verifier shares')

        # Verify the FLP.
        verifier = vec_add(verifier_share_0, verifier_share_1)
        if not self.flp.decide(verifier):
            raise Exception('FLP verification failed')

        if self.flp.JOINT_RAND_LEN == 0:
            return None
        if joint_rand_part_0 is None or joint_rand_part_1 is None:
            raise ValueError('expected FLP joint randomness parts')

        # Confirm the FLP joint randomness was computed properly.
        prep_msg = self.joint_rand_seed(ctx, [
            joint_rand_part_0,
            joint_rand_part_1,
        ])
        return prep_msg

    def prep_next(self,
                  _ctx: bytes,
                  prep_state: MasticPrepState,
                  prep_msg: MasticPrepMessage,
                  ) -> list[F]:
        (truncated_out_share, joint_rand_seed) = prep_state
        if joint_rand_seed is not None:
            if prep_msg is None:
                raise ValueError('expected joint rand confirmation')

            if prep_msg != joint_rand_seed:
                raise Exception('joint rand confirmation failed')

        return truncated_out_share

    def agg_init(self, agg_param: MasticAggParam) -> list[F]:
        (_level, prefixes, _do_weight_check) = agg_param
        agg = self.field.zeros(len(prefixes)*(1+self.flp.OUTPUT_LEN))
        return agg

    def agg_update(self,
                   agg_param: MasticAggParam,
                   agg_share: list[F],
                   out_share: list[F]) -> list[F]:
        return vec_add(agg_share, out_share)

    def merge(self,
              agg_param: MasticAggParam,
              agg_shares: list[list[F]]) -> list[F]:
        (_level, prefixes, _do_weight_check) = agg_param
        agg = self.agg_init(agg_param)
        for agg_share in agg_shares:
            agg = vec_add(agg, agg_share)
        return cast(list[F], agg)

    def unshard(self,
                agg_param: MasticAggParam,
                agg_shares: list[list[F]],
                _num_measurements: int,
                ) -> list[R]:
        agg = self.merge(agg_param, agg_shares)

        agg_result = []
        while len(agg) > 0:
            (chunk, agg) = front(self.flp.OUTPUT_LEN + 1, agg)
            meas_count = chunk[0].as_unsigned()
            agg_result.append(self.flp.decode(chunk[1:], meas_count))
        return agg_result

    def encode_agg_param(self, agg_param: MasticAggParam) -> bytes:
        (level, prefixes, do_weight_check) = agg_param
        if level not in range(2 ** 16):
            raise ValueError('level out of range')
        if len(prefixes) not in range(2 ** 32):
            raise ValueError('number of prefixes out of range')
        encoded = bytes()
        encoded += to_be_bytes(level, 2)
        encoded += to_be_bytes(len(prefixes), 4)
        # NOTE: The do_weight_check is the only difference between Mastic's and
        # Poplar1's `encode_agg_param``
        encoded += to_be_bytes(int(do_weight_check), 1)
        prefixes_len = ((level + 1) + 7) // 8 * len(prefixes)
        encoded_prefixes = bytearray()
        for prefix in prefixes:
            for chunk in itertools.batched(prefix, 8):
                byte_out = 0
                for (bit_position, bit) in enumerate(chunk):
                    byte_out |= bit << (7 - bit_position)
                encoded_prefixes.append(byte_out)
        assert len(encoded_prefixes) == prefixes_len
        encoded += encoded_prefixes
        return encoded

    def expand_input_share(
            self,
            ctx: bytes,
            agg_id: int,
            input_share: MasticInputShare,
    ) -> tuple[bytes, list[F], Optional[bytes]]:
        if agg_id == 0:
            (key, proof_share, seed, beta_share) = input_share
            assert proof_share is not None
        else:
            (key, _leader_proof_share, seed, beta_share) = input_share
            assert seed is not None
            proof_share = self.helper_proof_share(ctx, seed)
        return (key, proof_share, seed, beta_share)

    def helper_proof_share(self, ctx, seed: bytes) -> list[F]:
        return self.xof.expand_into_vec(
            self.field,
            seed,
            dst(ctx, USAGE_PROOF_SHARE),
            b'',
            self.flp.PROOF_LEN,
        )

    def prove_rand(self, ctx: bytes, seed: bytes) -> list[F]:
        return self.xof.expand_into_vec(
            self.field,
            seed,
            dst(ctx, USAGE_PROVE_RAND),
            b'',
            self.flp.PROVE_RAND_LEN,
        )

    def joint_rand_part(
            self,
            ctx: bytes,
            agg_id: int,
            seed: bytes,
            key: bytes,
            correction_words: list[CorrectionWord],
            nonce: bytes,
    ) -> bytes:
        pub = self.vidpf.encode_public_share(correction_words)
        return self.xof.derive_seed(
            seed,
            dst(ctx, USAGE_JOINT_RAND_PART),
            byte(agg_id) + nonce + key + pub,
        )

    def joint_rand_seed(self, ctx: bytes, parts: list[bytes]) -> bytes:
        return self.xof.derive_seed(
            zeros(self.xof.SEED_SIZE),
            dst(ctx, USAGE_JOINT_RAND_SEED),
            concat(parts),
        )

    def joint_rand(self, ctx: bytes, seed: bytes) -> list[F]:
        return self.xof.expand_into_vec(
            self.field,
            seed,
            dst(ctx, USAGE_JOINT_RAND),
            b'',
            self.flp.JOINT_RAND_LEN,
        )

    def query_rand(self,
                   verify_key: bytes,
                   ctx: bytes,
                   nonce: bytes,
                   level: int) -> list[F]:
        return self.xof.expand_into_vec(
            self.field,
            verify_key,
            dst(ctx, USAGE_QUERY_RAND),
            nonce + to_le_bytes(level, 2),
            self.flp.QUERY_RAND_LEN,
        )

    def beta_share_from_seed(self, ctx, seed: bytes) -> list[F]:
        return self.xof.expand_into_vec(
            self.field,
            seed,
            dst(ctx, USAGE_PROOF_SHARE),
            b'',
            self.flp.MEAS_LEN,
        )

    def test_vec_encode_input_share(
        self,
        input_share: MasticInputShare,
    ) -> bytes:
        (init_seed, proof_share, seed, beta_share) = input_share
        encoded = bytes()
        encoded += init_seed
        if proof_share is not None:
            encoded += self.field.encode_vec(proof_share)
        if seed is not None:
            encoded += seed
        if beta_share is not None:
            encoded += self.field.encode_vec(beta_share)
        return encoded

    def test_vec_encode_public_share(
        self,
        public_share: MasticPublicShare,
    ) -> bytes:
        (correction_words, joint_rand_parts) = public_share
        encoded = bytes()
        encoded += self.vidpf.encode_public_share(correction_words)
        if joint_rand_parts is not None:
            for seed in joint_rand_parts:
                encoded += seed
        return encoded

    def test_vec_encode_agg_share(self, agg_share: list[F]) -> bytes:
        encoded = bytes()
        if len(agg_share) > 0:
            encoded += self.field.encode_vec(agg_share)
        return encoded

    def test_vec_encode_prep_share(
            self, prep_share: MasticPrepShare) -> bytes:
        (eval_proof, verifier_share, joint_rand) = prep_share
        assert verifier_share is not None
        assert isinstance(verifier_share, list)
        encoded = bytes()
        encoded += eval_proof
        if len(verifier_share) > 0:
            encoded += self.field.encode_vec(verifier_share)
        if joint_rand is not None:
            encoded += joint_rand
        return encoded

    def test_vec_encode_prep_msg(
            self, prep_message: MasticPrepMessage) -> bytes:
        encoded = bytes()
        if prep_message is not None:
            encoded += prep_message
        return encoded


##
# INSTANTIATIONS
#

class MasticCount(Mastic):
    ID = 0xFFFF0001

    # Name of the VDAF, for use in test vector filenames.
    test_vec_name = 'MasticCount'

    def __init__(self, bits: int):
        super().__init__(bits, Count(Field64))


class MasticSum(Mastic):
    ID = 0xFFFF0002

    # Name of the VDAF, for use in test vector filenames.
    test_vec_name = 'MasticSum'

    def __init__(self, bits: int, max_measurement: int):
        super().__init__(bits, Sum(Field64, max_measurement))


class MasticSumVec(Mastic):
    ID = 0xFFFF0003

    # Name of the VDAF, for use in test vector filenames.
    test_vec_name = 'MasticSumVec'

    def __init__(self, bits: int, length: int, sum_vec_bits: int, chunk_length: int):
        super().__init__(bits, SumVec(Field128, length, sum_vec_bits, chunk_length))


class MasticHistogram(Mastic):
    ID = 0xFFFF0004

    # Name of the VDAF, for use in test vector filenames.
    test_vec_name = 'MasticHistogram'

    def __init__(self, bits: int, length: int, chunk_length: int):
        super().__init__(bits, Histogram(Field128, length, chunk_length))


class MasticMultihotCountVec(Mastic):
    ID = 0xFFFF0005

    # Name of the VDAF, for use in test vector filenames.
    test_vec_name = 'MasticMultihotCountVec'

    def __init__(self, bits: int, length: int, max_weight: int, chunk_length: int):
        super().__init__(bits, MultihotCountVec(Field128, length, max_weight, chunk_length))
