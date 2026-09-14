import unittest

from tokexchange import crypto


class CryptoTests(unittest.TestCase):
    def test_null_sealer_refuses_ciphertext(self):
        s = crypto.NullSealer()
        self.assertEqual(s.open(s.seal(b"abc")), b"abc")
        with self.assertRaises(crypto.SealError):
            s.open(crypto.MAGIC + b"garbage")

    def test_aes_roundtrip(self):
        try:
            import cryptography  # noqa: F401
        except ImportError:
            self.skipTest("cryptography not installed")
        a = crypto.AesGcmSealer("correct horse")
        blob = a.seal(b"payload" * 100)
        self.assertTrue(blob.startswith(crypto.MAGIC))
        self.assertEqual(a.open(blob), b"payload" * 100)
        with self.assertRaises(crypto.SealError):
            crypto.AesGcmSealer("wrong").open(blob)
        with self.assertRaises(crypto.SealError):
            a.open(b"plaintext")  # a configured secret must refuse clear payloads
        self.assertIsInstance(crypto.sealer_for(None), crypto.NullSealer)
