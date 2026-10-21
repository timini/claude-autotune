import time
import unittest

from autotune.redact import redact


class RedactPatternTests(unittest.TestCase):
    def assertRedacted(self, text, secret, kind):
        out, count = redact(text)
        self.assertNotIn(secret, out)
        self.assertIn(f"[REDACTED:{kind}]", out)
        self.assertGreaterEqual(count, 1)
        return out, count

    def test_pem_private_key(self):
        body = "MIIEowIBAAKCAQEA1234567890abcdef"
        text = f"key:\n-----BEGIN RSA PRIVATE KEY-----\n{body}\n-----END RSA PRIVATE KEY-----\nafter"
        out, count = self.assertRedacted(text, body, "private_key")
        self.assertEqual(count, 1)
        self.assertTrue(out.endswith("\nafter"))

    def test_truncated_pem_redacts_to_end(self):
        text = "x -----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk"
        out, _ = self.assertRedacted(text, "b3BlbnNzaC1rZXk", "private_key")
        self.assertEqual(out, "x [REDACTED:private_key]")

    def test_token_shapes(self):
        cases = [
            ("AKIAIOSFODNN7EXAMPLE", "aws_access_key"),
            ("ASIAIOSFODNN7EXAMPLE", "aws_access_key"),
            ("ghp_" + "A" * 36, "github_token"),
            ("gho_" + "b" * 36, "github_token"),
            ("ghs_" + "c" * 36, "github_token"),
            ("ghu_" + "d" * 36, "github_token"),
            ("github_pat_" + "E1_" * 20, "github_token"),
            ("sk-ant-api03-" + "f" * 40, "anthropic_key"),
            ("sk-proj-" + "g" * 40, "api_key"),
            ("xoxb-1234567890-abcdefghij", "slack_token"),
            ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.dBjftJeZ4CVPmB92K27uhbUJU1p1r", "jwt"),
        ]
        for secret, kind in cases:
            with self.subTest(kind=kind, secret=secret[:8]):
                out, count = self.assertRedacted(f"value is {secret} here", secret, kind)
                self.assertEqual(count, 1)
                self.assertEqual(out, f"value is [REDACTED:{kind}] here")

    def test_bearer_header_keeps_header_name(self):
        out, count = self.assertRedacted("Authorization: Bearer abc.def-123", "abc.def-123",
                                         "bearer_token")
        self.assertTrue(out.startswith("Authorization: Bearer "))
        self.assertEqual(count, 1)

    def test_keyword_values(self):
        for text, secret in [
            ("password=hunter22", "hunter22"),
            ("DB_PASSWD: s3cr3t!", "s3cr3t!"),
            ("client_secret = 'abc123xyz'", "abc123xyz"),
            ('"api_key": "k-998877"', "k-998877"),
            ("GITHUB_TOKEN=plainvalue", "plainvalue"),
        ]:
            with self.subTest(text=text):
                out, count = self.assertRedacted(text, secret, "credential")
                self.assertEqual(count, 1)

    def test_keyword_does_not_double_count_known_tokens(self):
        out, count = redact("token=ghp_" + "Z" * 36)
        self.assertEqual(count, 1)
        self.assertEqual(out, "token=[REDACTED:github_token]")

    def test_ordinary_text_untouched(self):
        text = ("The tokenizer counts tokens. Reset your password in settings. "
                "sk-learn is a library. Use AKIA as a prefix example. eyJ alone.")
        self.assertEqual(redact(text), (text, 0))

    def test_empty(self):
        self.assertEqual(redact(""), ("", 0))

    def test_multiple_secrets_counted(self):
        text = "AKIAIOSFODNN7EXAMPLE and AKIAIOSFODNN7EXAMPLF and password=x1"
        _, count = redact(text)
        self.assertEqual(count, 3)


class RedactPerformanceTests(unittest.TestCase):
    def test_near_miss_megabyte_is_fast_and_untouched(self):
        pieces = [
            "AKIA123456789012345 ",
            "sk-short1234 ",
            "ghp_abc ",
            "eyJabcde.fghij ",
            "password and ",
            "-----BEGIN PUBLIC KEY----- ",
            "xoxb-1 ",
            "Authorization: Basic ",
            "eyJeyJeyJeyJ",
            "sk-sk-sk-",
            "____",
            '\\"\\"',
        ]
        unit = "".join(pieces)
        text = (unit * (1024 * 1024 // len(unit) + 1))[: 1024 * 1024]
        start = time.perf_counter()
        out, count = redact(text)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 2.0)
        self.assertEqual(count, 0)
        self.assertEqual(out, text)

    def test_pathological_runs_are_fast(self):
        for text in ("eyJ" * 350000, "-----BEGIN RSA PRIVATE KEY-----" * 30000,
                     "a" * 1000000 + ".", "token" * 200000, "_a" * 500000):
            with self.subTest(prefix=text[:10]):
                start = time.perf_counter()
                redact(text)
                self.assertLess(time.perf_counter() - start, 2.0)


if __name__ == "__main__":
    unittest.main()


class RedactJsonEscapedTests(unittest.TestCase):
    def check(self, command, secret):
        import json

        text = json.dumps({"command": command})
        out, count = redact(text)
        self.assertNotIn(secret, out, out)
        self.assertGreaterEqual(count, 1)
        return out

    def test_password_inside_escaped_quotes(self):
        self.check('mysql --password="hunter2secret" db', "hunter2secret")

    def test_single_quoted_password(self):
        self.check("mysql --password='hunter2secret' db", "hunter2secret")

    def test_prefixed_secret_env_names(self):
        self.check("AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY aws s3 ls", "wJalrXUtnFEMIK7MDENG")
        self.check("SECRET_KEY=abcdef0123456789 ./run", "abcdef0123456789")
        self.check("export GITHUB_TOKEN=abcdef0123456789xyz", "abcdef0123456789xyz")
        self.check("DB_PASSWORD: s3cr3tvalue", "s3cr3tvalue")

    def test_url_userinfo(self):
        out = self.check("psql postgres://admin:pa55word@db.internal:5432/app", "pa55word")
        self.assertIn("db.internal", out)

    def test_common_non_secrets_are_left_alone(self):
        for text in ('{"max_tokens": 4096}', "tokenizer=bpe", "https://example.com/path", "git@github.com:o/r.git"):
            with self.subTest(text=text):
                self.assertEqual(redact(text), (text, 0))
