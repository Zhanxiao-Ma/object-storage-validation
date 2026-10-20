"""Exercise the real migrated Ceph generator with independent boundary oracles."""
import unittest
from osv.ceph_utils import generate_random,generate


class CephUtilsTest(unittest.TestCase):
    def test_original_five_sizes(self):
        """Purpose: execute the production source's complete original five assertions; expect exact sizes and explicit no-cloud evidence."""
        result=generate(None)
        self.assertEqual(result,{'execution_kind':'local_utility','cloud_requests':0,'original_sizes':[0,1,5242879,5242880,5242881]})

    def test_generator_chunk_and_remainder_boundaries(self):
        """Purpose: consume the real producer directly at part/chunk edges; expect ASCII letters, exact per-part sizes and no dropped remainder."""
        for size,part,expected in ((0,7,[]),(1,7,[1]),(6,7,[6]),(7,7,[7]),(8,7,[7,1]),(1023,1024,[1023]),(1024,1024,[1024]),(1025,1024,[1024,1]),(2049,1024,[1024,1024,1])):
            with self.subTest(size=size,part=part):
                rows=list(generate_random(size,part))
                self.assertEqual([len(row) for row in rows],expected)
                self.assertTrue(all(c in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ' for row in rows for c in row))


if __name__=='__main__':unittest.main()
