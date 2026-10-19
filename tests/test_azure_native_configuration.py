from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from kwikledgers_fastmcp.domains.azure_devops.configuration import get_client, get_project, load_configuration


class NativeConfigurationTests(unittest.TestCase):
    def test_system_bundle_replaces_existing_inherited_isolated_certificates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "ca-certificates.crt"
            bundle.write_text("system bundle\n", encoding="utf-8")
            isolated = root / "zscaler.crt"
            isolated.write_text("isolated CA\n", encoding="utf-8")
            names = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "AZURE_CA_BUNDLE", "CURL_CA_BUNDLE")
            with patch.dict(os.environ, {name: str(isolated) for name in names}, clear=True):
                with patch("kwikledgers_fastmcp.domains.azure_devops.configuration.workspace_root", return_value=root):
                    with patch("kwikledgers_fastmcp.domains.azure_devops.configuration._SYSTEM_CA_BUNDLE", bundle):
                        load_configuration()
                        for name in names:
                            self.assertEqual(os.environ[name], str(bundle), name)
                        with requests.Session() as session:
                            settings = session.merge_environment_settings(
                                "https://dev.azure.com/example", {}, False, True, None,
                            )
                        self.assertEqual(settings["verify"], str(bundle))

    def test_system_bundle_overrides_isolated_certificates_loaded_from_dotenv(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "ca-certificates.crt"
            bundle.write_text("system bundle\n", encoding="utf-8")
            isolated = root / "zscaler.crt"
            isolated.write_text("isolated CA\n", encoding="utf-8")
            env_file = root / ".env"
            env_file.write_text(f"REQUESTS_CA_BUNDLE={isolated}\nAZURE_CA_BUNDLE={isolated}\n", encoding="utf-8")
            with patch.dict(os.environ, {"KWIKLEDGERS_ENV_FILE": str(env_file)}, clear=True):
                with patch("kwikledgers_fastmcp.domains.azure_devops.configuration._SYSTEM_CA_BUNDLE", bundle):
                    load_configuration()
                    self.assertEqual(os.environ["REQUESTS_CA_BUNDLE"], str(bundle))
                    self.assertEqual(os.environ["AZURE_CA_BUNDLE"], str(bundle))

    def test_missing_system_bundle_preserves_platform_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            names = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "AZURE_CA_BUNDLE", "CURL_CA_BUNDLE")
            configured = {name: str(root / "custom-ca.pem") for name in names}
            with patch.dict(os.environ, configured, clear=True):
                with patch("kwikledgers_fastmcp.domains.azure_devops.configuration.workspace_root", return_value=root):
                    with patch("kwikledgers_fastmcp.domains.azure_devops.configuration._SYSTEM_CA_BUNDLE", root / "missing"):
                        load_configuration()
                        for name in names:
                            self.assertEqual(os.environ[name], configured[name], name)

    def test_empty_system_bundle_fails_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "ca-certificates.crt"
            bundle.touch()
            with patch.dict(os.environ, {}, clear=True):
                with patch("kwikledgers_fastmcp.domains.azure_devops.configuration.workspace_root", return_value=root):
                    with patch("kwikledgers_fastmcp.domains.azure_devops.configuration._SYSTEM_CA_BUNDLE", bundle):
                        with self.assertRaisesRegex(ValueError, "TLS system CA bundle is empty"):
                            load_configuration()

    def test_explicit_environment_does_not_override_exported_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text("AZURE_PROJECT=File Project\n", encoding="utf-8")
            with patch.dict(os.environ, {"KWIKLEDGERS_ENV_FILE": str(env_file), "AZURE_PROJECT": "Exported"}, clear=True):
                self.assertEqual(load_configuration(), env_file)
                self.assertEqual(get_project(), "Exported")

    def test_missing_credentials_are_not_disclosed(self) -> None:
        with patch("kwikledgers_fastmcp.domains.azure_devops.configuration.load_configuration"):
            with patch.dict(os.environ, {"AZURE_PAT": "secret-value"}, clear=True):
                with self.assertRaisesRegex(ValueError, "AZURE_ORG_URL") as raised:
                    get_client()
                self.assertNotIn("secret-value", str(raised.exception))

    def test_connection_is_lazy(self) -> None:
        with patch("kwikledgers_fastmcp.domains.azure_devops.configuration.load_configuration"):
            with patch.dict(os.environ, {"AZURE_ORG_URL": "https://dev.azure.com/example", "AZURE_PAT": "test"}):
                with patch("azure.devops.connection.Connection") as connection:
                    self.assertIs(get_client(), connection.return_value)
                    connection.assert_called_once()