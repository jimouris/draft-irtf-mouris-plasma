"""Verifiable Distributed Point Function (VIDPF)"""

import sys

sys.path.append('draft-irtf-cfrg-vdaf/poc')  # nopep8

import hashlib

from common import (format_dst, gen_rand, to_le_bytes, vec_add, vec_neg,
                    vec_sub, xor)
from field import Field2, Field128
from xof import XofFixedKeyAes128


class Vidpf:
    """A Verifiable Distributed Point Function (VIDPF)."""

    # Operational parameters.
    Field = None  # set by `with_params()`
    ROOT_PROOF = hashlib.sha3_256().digest()  # Hash of the empty string

    # Bit length of valid input values (i.e., the length of `alpha` in bits).
    BITS = None  # set by `with_params()`

    # The length of each output vector (i.e., the length of `beta_leaf`).
    VALUE_LEN = None  # set by `with_params()`

    # Constants

    # Number of keys generated by the vidpf-key generation algorithm.
    SHARES = 2

    # Size in bytes of each vidpf key share.
    KEY_SIZE = XofFixedKeyAes128.SEED_SIZE

    # Number of random bytes consumed by the `gen()` algorithm.
    RAND_SIZE = 2 * XofFixedKeyAes128.SEED_SIZE

    @classmethod
    def gen(cls, alpha, beta, binder, rand):
        '''
        https://eprint.iacr.org/2023/080.pdf VIDPF.Gen
        '''
        if alpha >= 2**cls.BITS:
            raise ValueError("alpha too long")
        if len(rand) != cls.RAND_SIZE:
            raise ValueError("randomness has incorrect length")

        init_seed = [
            rand[:XofFixedKeyAes128.SEED_SIZE],
            rand[XofFixedKeyAes128.SEED_SIZE:],
        ]

        # s0^0, s1^0, t0^0, t1^0
        seed = init_seed.copy()
        ctrl = [Field2(0), Field2(1)]
        correction_words = []
        cs_proofs = []
        for i in range(cls.BITS):
            node = (alpha >> (cls.BITS - i - 1))
            bit = node & 1
            # if x = 0 then keep <- L, lose <- R
            keep, lose = (1, 0) if bit else (0, 1)

            # s_0^L || s_0^R || t_0^L || t_0^R
            (s_0, t_0) = cls.extend(seed[0], binder)
            # s_1^L || s_1^R || t_1^L || t_1^R
            (s_1, t_1) = cls.extend(seed[1], binder)
            seed_cw = xor(s_0[lose], s_1[lose])
            ctrl_cw = (
                t_0[0] + t_1[0] + Field2(1) + Field2(bit),  # t_c^L
                t_0[1] + t_1[1] + Field2(bit),             # t_c^R
            )

            (seed[0], w_0) = cls.convert(
                correct(s_0[keep], seed_cw, ctrl[0]), i, binder)
            (seed[1], w_1) = cls.convert(
                correct(s_1[keep], seed_cw, ctrl[1]), i, binder)
            ctrl[0] = correct(t_0[keep], ctrl_cw[keep], ctrl[0])  # t0'
            ctrl[1] = correct(t_1[keep], ctrl_cw[keep], ctrl[1])  # t1'

            w_cw = vec_add(vec_sub(beta, w_0), w_1)
            mask = cls.Field(1) - cls.Field(2) * \
                cls.Field(ctrl[1].as_unsigned())
            for j in range(len(w_cw)):
                w_cw[j] *= mask

            # Compute hashes for level i
            sha3 = hashlib.sha3_256()
            sha3.update(str(node).encode('ascii') +
                        to_le_bytes(i, 2) + seed[0])
            proof_0 = sha3.digest()
            sha3 = hashlib.sha3_256()
            sha3.update(str(node).encode('ascii') +
                        to_le_bytes(i, 2) + seed[1])
            proof_1 = sha3.digest()

            cs_proofs.append(xor(proof_0, proof_1))
            correction_words.append((seed_cw, ctrl_cw, w_cw))

        return (init_seed, correction_words, cs_proofs)

    @classmethod
    def eval(cls,
             agg_id,
             correction_words,  # public share
             cs_proofs,        # public share
             init_seed,        # key share
             level,
             prefixes,
             binder):
        if agg_id >= cls.SHARES:
            raise ValueError("invalid aggregator ID")
        if level >= cls.BITS:
            raise ValueError("level too deep")
        if len(set(prefixes)) != len(prefixes):
            raise ValueError("candidate prefixes are non-unique")

        # Compute the Aggregator's share of the prefix tree and the one-hot
        # proof (`pi_proof`).
        #
        # Implementation note: We can save computation by storing
        # `prefix_tree_share` across `eval()` calls for the same report.
        pi_proof = cls.ROOT_PROOF
        prefix_tree_share = {}
        for prefix in prefixes:
            if prefix >= 2 ** (level+1):
                raise ValueError("prefix too long")

            # The Aggregator's output share is the value of a node of
            # the IDPF tree at the given `level`. The node's value is
            # computed by traversing the path defined by the candidate
            # `prefix`. Each node in the tree is represented by a seed
            # (`seed`) and a set of control bits (`ctrl`).
            seed = init_seed
            ctrl = Field2(agg_id)
            for current_level in range(level+1):
                node = prefix >> (level - current_level)
                for s in [0, 1]:
                    # Compute the value for the node `node` and its sibling
                    # `node ^ s`. The latter is used for computing the path
                    # proof.
                    if not prefix_tree_share.get((node ^ s, current_level)):
                        prefix_tree_share[(node ^ s, current_level)] = cls.eval_next(
                            seed,
                            ctrl,
                            correction_words[current_level],
                            cs_proofs[current_level],
                            current_level,
                            node ^ s,
                            pi_proof,
                            binder,
                        )
                (seed, ctrl, y, pi_proof) = prefix_tree_share.get(
                    (node, current_level))

        # Compute the path proof.
        sha3 = hashlib.sha3_256()
        for prefix in prefixes:
            for current_level in range(level):
                node = prefix >> (level - current_level)
                y = prefix_tree_share[(node,        current_level)][2]
                y0 = prefix_tree_share[(node << 1,     current_level+1)][2]
                y1 = prefix_tree_share[((node << 1) | 1, current_level+1)][2]
                sha3.update(cls.Field.encode_vec(vec_sub(y, vec_add(y0, y1))))
        path_proof = sha3.digest()

        # Compute the Aggregator's output share.
        out_share = []
        for prefix in prefixes:
            (_seed, _ctrl, y, _pi_proof) = prefix_tree_share[(prefix, level)]
            out_share.append(y if agg_id == 0 else vec_neg(y))

        # Compute the Aggregator's share of `beta`.
        y0 = prefix_tree_share[(0, 0)][2]
        y1 = prefix_tree_share[(1, 0)][2]
        beta_share = vec_add(y0, y1)
        if agg_id == 1:
            beta_share = vec_neg(beta_share)
        return (beta_share, out_share, pi_proof + path_proof)

    @classmethod
    def eval_next(cls, prev_seed, prev_ctrl, correction_word, cs_proof,
                  current_level, node, pi_proof, binder):
        """
        Compute the next node in the VIDPF tree along the path determined by
        a candidate prefix. The next node is determined by `bit`, the bit of
        the prefix corresponding to the next level of the tree.
        """
        (seed_cw, ctrl_cw, w_cw) = correction_word

        # (s^L, s^R), (t^L, t^R) = PRG(s^{i-1})
        (s, t) = cls.extend(prev_seed, binder)
        s[0] = xor(s[0], prev_ctrl.conditional_select(seed_cw))  # s^L
        s[1] = xor(s[1], prev_ctrl.conditional_select(seed_cw))  # s^R
        t[0] += ctrl_cw[0] * prev_ctrl  # t^L
        t[1] += ctrl_cw[1] * prev_ctrl  # t^R

        bit = node & 1
        next_ctrl = t[bit]  # t'^i
        (next_seed, w) = cls.convert(s[bit], current_level, binder)  # s^i, W^i
        # Implementation note: Here we add the correction word to the
        # output if `next_ctrl` is set. We avoid branching on the value of
        # the control bit in order to reduce side channel leakage.
        y = []
        mask = cls.Field(next_ctrl.as_unsigned())
        for i in range(len(w)):
            y.append(w[i] + w_cw[i] * mask)

        sha3 = hashlib.sha3_256()
        # pi' = H(x^{<= i} || s^i)
        sha3.update(str(node).encode('ascii') +
                    to_le_bytes(current_level, 2) + next_seed)
        pi_prime = sha3.digest()

        # \pi = \pi xor H(\pi \xor (proof_prime \xor next_ctrl * cs_proof))
        sha3 = hashlib.sha3_256()
        if next_ctrl.as_unsigned() == 1:
            h2 = xor(pi_proof, xor(pi_prime, cs_proof))
        else:
            h2 = xor(pi_proof, pi_prime)
        sha3.update(h2)
        pi_proof = xor(pi_proof, sha3.digest())

        return (next_seed, next_ctrl, y, pi_proof)

    @classmethod
    def verify(cls, proof_0, proof_1):
        '''Check proofs'''
        return proof_0 == proof_1

    @classmethod
    def extend(cls, seed, binder):
        '''
        Extend seed to (seed_L, t_L, seed_R, t_R)
        '''
        xof = XofFixedKeyAes128(seed, format_dst(1, 0, 0), binder)
        new_seed = [
            xof.next(XofFixedKeyAes128.SEED_SIZE),
            xof.next(XofFixedKeyAes128.SEED_SIZE),
        ]
        bit = xof.next(1)[0]
        ctrl = [Field2(bit & 1), Field2((bit >> 1) & 1)]
        return (new_seed, ctrl)

    @classmethod
    def convert(cls, seed, level, binder):
        # TODO(jimouris): level is currently unused.
        '''
        Converting seed to a pseudorandom element of G.
        '''
        xof = XofFixedKeyAes128(seed, format_dst(1, 0, 1), binder)
        next_seed = xof.next(XofFixedKeyAes128.SEED_SIZE)
        # TODO(cjpatton) This is slightly abusing the `Prg` API, as
        # `next_vec()` expects a `Field` as its first parameter. Either
        # re-implement the method here (if the ring modulus is a power of 2,
        # then this should be quite easy) or update the `Prg` upstream to take
        # a `Ring` and make `Field` a subclass of `Ring`.
        return (next_seed, xof.next_vec(cls.Field, cls.VALUE_LEN))

    @classmethod
    def with_params(cls, f, bits, value_len):
        class VdipfWithField(cls):
            Field = f
            BITS = bits
            VALUE_LEN = value_len
        return VdipfWithField


def correct(k_0, k_1, ctrl):
    ''' return k_0 if ctrl == 0 else xor(k_0, k_1) '''
    if isinstance(k_0, bytes):
        return xor(k_0, ctrl.conditional_select(k_1))
    if isinstance(k_0, list):  # list of ints or ring elements
        for i in range(len(k_0)):
            k_0[i] += ctrl * k_1[i]
        return k_0
    # int or ring element
    return k_0 + ctrl * k_1


def main():
    '''Driver'''
    vidpf = Vidpf.with_params(Field128, 2, 1)

    binder = b'some nonce'
    # alpha values from different users
    measurements = [0b10, 0b00, 0b11, 0b01, 0b11]
    beta = [vidpf.Field(1)]
    prefixes = [0b0, 0b1]
    level = 0

    sha3 = hashlib.sha3_256()
    sha3.update(str(0).encode('ascii'))

    out = [Field128.zeros(vidpf.VALUE_LEN)] * len(prefixes)
    for measurement in measurements:
        rand = gen_rand(vidpf.RAND_SIZE)
        init_seed, correction_words, cs_proofs = vidpf.gen(
            measurement, beta, binder, rand)

        proofs = []
        for agg_id in range(vidpf.SHARES):
            (_beta_share, out_share, proof) = vidpf.eval(
                agg_id,
                correction_words,
                cs_proofs,
                init_seed[agg_id],
                level,
                prefixes,
                binder,
            )
            proofs.append(proof)

            for i in range(len(prefixes)):
                out[i] = vec_add(out[i], out_share[i])
        assert vidpf.verify(proofs[0], proofs[1])

    print('Aggregated:', out)
    assert out == [[Field128(2)], [Field128(3)]]

    vidpf = Vidpf.with_params(Field128, 16, 1)
    # `alpha` values from different Clients.
    measurements = [
        0b1111000011110000,
        0b1111000011110001,
        0b1111000011110010,
        0b0000010011110010,
    ]
    beta = [Field128(1)]
    prefixes = [
        0b000001,
        0b111100,
        0b111101,
    ]
    level = 5

    sha3 = hashlib.sha3_256()
    sha3.update(str(0).encode('ascii'))

    out = [Field128.zeros(vidpf.VALUE_LEN)] * len(prefixes)
    for measurement in measurements:
        rand = gen_rand(vidpf.RAND_SIZE)
        init_seed, correction_words, cs_proofs = vidpf.gen(
            measurement, beta, binder, rand)

        proofs = []
        for agg_id in range(vidpf.SHARES):
            (_beta_share, out_share, proof) = vidpf.eval(
                agg_id,
                correction_words,
                cs_proofs,
                init_seed[agg_id],
                level,
                prefixes,
                binder,
            )
            proofs.append(proof)

            for i in range(len(prefixes)):
                out[i] = vec_add(out[i], out_share[i])
        assert vidpf.verify(proofs[0], proofs[1])

    print('Aggregated:', out)
    assert out == [[Field128(1)], [Field128(3)], [Field128(0)]]


if __name__ == '__main__':
    main()
