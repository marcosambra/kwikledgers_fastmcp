from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from kwikledgers_fastmcp.domains.azure_devops import services
from kwikledgers_fastmcp.domains.azure_devops.services import _format_azure_rich_text


class AzureRichTextTests(unittest.TestCase):
    def test_safe_generated_html_is_unchanged(self) -> None:
        fragments = [
            "<h3>Technical debt</h3><p>Impact<br>Next step<br/>Owner<br />Done</p>",
            "<p><strong>Summary</strong> and <em>impact</em> with <code>name</code>.</p>",
            "<ul><li>One</li><li>Two</li></ul><ol><li>Three</li></ol>",
            '<pre><code class="language-c++">&lt;value&gt;</code></pre>',
            "<pre><code class='language-python_3'>print(1)</code></pre>",
            "<p>&lt;script&gt;alert(1)&lt;/script&gt; &#60;script&#62;</p>",
            *(f"<h{level}>Heading</h{level}>" for level in range(1, 7)),
        ]
        for fragment in fragments:
            with self.subTest(fragment=fragment):
                self.assertEqual(_format_azure_rich_text(fragment), fragment)

    def test_unsafe_or_malformed_html_is_escaped(self) -> None:
        fragments = [
            '<p onclick="alert(1)">Text</p>',
            '<p style="color:red">Text</p>',
            '<code class="language-python" onmouseover="alert(1)">Text</code>',
            '<pre><code class="language-python extra">Text</code></pre>',
            '<pre><code class="language-python" class="language-js">Text</code></pre>',
            '<p><a href="javascript:alert(1)">Link</a></p>',
            '<link rel="stylesheet" href="https://example.test">',
            "<script>alert(1)</script>",
            "<style>body{display:none}</style>",
            "<!DOCTYPE html><p>Text</p>",
            "<!-- comment --><p>Text</p>",
            "<?instruction?><p>Text</p>",
            "<![CDATA[<script>alert(1)</script>]]><p>Text</p>",
            "<p><h3>Invalid nesting</h3></p>",
            "<ul><p>Invalid list</p></ul>",
            "<p><strong>Mismatch</p></strong>",
            "<p>Unclosed",
            "<p>Text</p extra>",
            "<p/>Text",
            "<p>Text</br></p>",
            "<p>Text</p> unwrapped text",
        ]
        for fragment in fragments:
            with self.subTest(fragment=fragment):
                rendered = _format_azure_rich_text(fragment)
                self.assertIsNotNone(rendered)
                self.assertIn("&lt;", rendered)
                self.assertNotEqual(rendered, fragment)
                self.assertEqual(rendered, services._render_lines_as_azure_html(
                    services._normalize_markdown_lines(fragment)
                ))

    def test_markdown_still_renders_and_is_idempotent(self) -> None:
        rendered = _format_azure_rich_text("# Summary\n\nText with `code`.\n\n- One\n- Two\n\n```python\nprint(1)\n```")
        self.assertEqual(rendered, '<p><strong>Summary</strong></p><p>Text with <code>code</code>.</p>'
                         '<ul><li>One</li><li>Two</li></ul><pre><code class="language-python">print(1)</code></pre>')
        self.assertEqual(_format_azure_rich_text(rendered), rendered)

    def test_empty_input(self) -> None:
        self.assertIsNone(_format_azure_rich_text(None))
        self.assertIsNone(_format_azure_rich_text(""))

    def test_existing_active_pr_is_rejected_before_any_writes(self) -> None:
        connection = MagicMock()
        preview = {"existing_active_pull_request": {"pull_request_id": 99}}
        with patch.object(services, "get_client", return_value=connection), \
                patch.object(services, "_resolve_git_repository"), \
                patch.object(services, "_build_story_pull_request_preview_payload", return_value=preview), \
                patch.object(services, "_check_association_revisions") as revisions, \
                patch.object(services, "_associate_artifact_to_work_items") as associate:
            with self.assertRaisesRegex(ValueError, "existing_active_pull_request"):
                services._create_story_pull_request(
                    story_id=1, repository_name="repo", source_branch="feature/KL-1-story",
                    what_was_changed="Changes", affected_processes="Process",
                    expected_impacts="Impact", important_points="Notes", mermaid_diagram="",
                )
        revisions.assert_not_called()
        associate.assert_not_called()
        connection.clients.get_git_client.return_value.create_pull_request.assert_not_called()
        connection.clients.get_work_item_tracking_client.return_value.update_work_item.assert_not_called()


if __name__ == "__main__":
    unittest.main()