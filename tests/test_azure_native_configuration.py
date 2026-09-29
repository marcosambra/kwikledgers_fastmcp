from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kwikledgers_fastmcp.domains.azure_devops.configuration import get_client, get_project, load_configuration


class NativeConfigurationTests(unittest.TestCase):
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