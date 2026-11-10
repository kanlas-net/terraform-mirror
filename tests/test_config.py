"""Configuration from the environment and from the command line."""

from __future__ import annotations

import unittest

from terraform_mirror.config import Config, S3Config, config_from_namespace, build_arg_parser
from terraform_mirror.errors import ConfigError


class EnvConfigTest(unittest.TestCase):
    def test_defaults(self):
        cfg = Config.from_env({})
        self.assertEqual(cfg.listen_port, 8080)
        self.assertEqual(cfg.backends, ["fs"])
        self.assertEqual(cfg.upstream_url, "https://registry.terraform.io")
        self.assertIsNone(cfg.s3)
        self.assertTrue(cfg.stale_revalidate)

    def test_env_overrides(self):
        cfg = Config.from_env(
            {
                "TFM_PORT": "9000",
                "TFM_BACKENDS": "fs, s3",
                "TFM_CACHE_DIR": "/var/cache/tfp",
                "TFM_TTL_VERSIONS": "60",
                "TFM_REQUEST_TIMEOUT": "3",
                "TFM_DOWNLOAD_TIMEOUT": "120",
                "TFM_STALE_REVALIDATE": "false",
                "TFM_UPSTREAM_URL": "https://registry.example.com/",
                "TFM_EXTERNAL_URL": "https://tf.example.com/",
                "TFM_S3_BUCKET": "my-bucket",
                "TFM_S3_REGION": "ru-central1",
                "TFM_S3_ENDPOINT": "https://storage.yandexcloud.net",
                "TFM_S3_PREFIX": "/tf/",
                "AWS_ACCESS_KEY_ID": "AKIA",
                "AWS_SECRET_ACCESS_KEY": "shhh",
            }
        )
        self.assertEqual(cfg.listen_port, 9000)
        self.assertEqual(cfg.backends, ["fs", "s3"])
        self.assertEqual(cfg.cache_dir, "/var/cache/tfp")
        self.assertEqual(cfg.ttl_versions, 60)
        self.assertEqual(cfg.request_timeout, 3.0)
        self.assertEqual(cfg.download_timeout, 120.0)
        self.assertFalse(cfg.stale_revalidate)
        self.assertEqual(cfg.upstream_url, "https://registry.example.com")
        self.assertEqual(cfg.external_base_url, "https://tf.example.com")
        self.assertEqual(cfg.s3.bucket, "my-bucket")
        self.assertEqual(cfg.s3.region, "ru-central1")
        self.assertEqual(cfg.s3.prefix, "tf")
        self.assertEqual(cfg.s3.access_key, "AKIA")
        self.assertTrue(cfg.s3.resolved_path_style())

    def test_aws_endpoint_defaults_to_virtual_host(self):
        s3 = S3Config(bucket="b", region="us-east-1")
        self.assertFalse(s3.resolved_path_style())
        s3 = S3Config(bucket="b", region="ru-central1", endpoint_url="https://storage.yandexcloud.net")
        self.assertTrue(s3.resolved_path_style())

    def test_validation_rejects_bad_backends(self):
        with self.assertRaises(ConfigError):
            Config(backends=["gcs"]).validate()
        with self.assertRaises(ConfigError):
            Config(backends=["fs", "fs"]).validate()
        with self.assertRaises(ConfigError):
            Config(backends=["s3"], s3=None).validate()

    def test_validation_rejects_half_a_credential_pair(self):
        with self.assertRaises(ConfigError):
            Config(backends=["s3"], s3=S3Config(bucket="bucket", access_key="ak")).validate()

    def test_validation_rejects_bad_ttl_and_port(self):
        with self.assertRaises(ConfigError):
            Config(ttl_versions=0).validate()
        with self.assertRaises(ConfigError):
            Config(listen_port=70000).validate()
        with self.assertRaises(ConfigError):
            Config(workers=0).validate()

    def test_shutdown_grace(self):
        self.assertEqual(Config.from_env({}).shutdown_grace, 15)
        self.assertEqual(Config.from_env({"TFM_SHUTDOWN_GRACE": "45"}).shutdown_grace, 45)
        with self.assertRaises(ConfigError):
            Config(shutdown_grace=-1).validate()

    def test_tls_needs_both_files(self):
        with self.assertRaises(ConfigError):
            Config(tls_cert="/tmp/cert.pem", tls_key="").validate()

    def test_public_base(self):
        cfg = Config(external_base_url="https://tf.example.com")
        self.assertEqual(cfg.public_base("ignored:1234"), "https://tf.example.com")
        cfg = Config(listen_host="0.0.0.0", listen_port=8080)
        self.assertEqual(cfg.public_base("proxy.local:8080"), "http://proxy.local:8080")
        self.assertEqual(cfg.public_base(), "http://127.0.0.1:8080")

    def test_bucket_name_is_validated(self):
        with self.assertRaises(ConfigError):
            S3Config(bucket="Bad_Name").validate()


class ArgsConfigTest(unittest.TestCase):
    def test_cli_beats_env(self):
        import os

        os.environ["TFM_PORT"] = "9999"
        try:
            cfg = Config.from_args(["--port", "1234", "--backend", "fs", "--ttl-versions", "120"])
        finally:
            del os.environ["TFM_PORT"]
        self.assertEqual(cfg.listen_port, 1234)
        self.assertEqual(cfg.ttl_versions, 120)
        self.assertEqual(cfg.backends, ["fs"])

    def test_timeout_flags_reach_the_clients(self):
        cfg = Config.from_args(["--backend", "fs", "--request-timeout", "2.5", "--download-timeout", "90"])
        self.assertEqual(cfg.request_timeout, 2.5)
        self.assertEqual(cfg.download_timeout, 90.0)

    def test_config_from_namespace_keeps_env_values(self):
        namespace = build_arg_parser().parse_args(["--backend", "s3", "--s3-bucket", "cli-bucket"])
        cfg = config_from_namespace(namespace)
        self.assertEqual(cfg.s3.bucket, "cli-bucket")
        self.assertEqual(cfg.listen_port, 8080)

    def test_s3_flags_only_apply_when_backend_selected(self):
        namespace = build_arg_parser().parse_args(["--s3-bucket", "unused"])
        cfg = config_from_namespace(namespace)
        self.assertIsNone(cfg.s3)

    def test_boolean_env_must_parse(self):
        """A typo must not silently switch a feature off (locks off is a real hazard)."""
        with self.assertRaises(ConfigError):
            Config.from_env({"TFM_LOCKS": "flase"})
        self.assertFalse(Config.from_env({"TFM_LOCKS": "off"}).locks_enabled)
        self.assertTrue(Config.from_env({"TFM_LOCKS": "yes"}).locks_enabled)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
