from __future__ import annotations

import asyncio
import ast
import copy
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from kwikledgers_fastmcp.domains.azure_devops import invoke_azure_capability
from kwikledgers_fastmcp.domains.azure_devops import services
from kwikledgers_fastmcp.domains.azure_devops.native import AzureCapabilityError
from kwikledgers_fastmcp.domains.azure_devops.primitives import work_item_revision


def item(work_item_id: int, title: str, kind: str = "User Story", state: str = "Active") -> Any:
    return SimpleNamespace(
        id=work_item_id, rev=7, url=f"https://example.test/wit/{work_item_id}", relations=[],
        fields={
            "System.Id": work_item_id, "System.Title": title, "System.WorkItemType": kind,
            "System.State": state, "System.IterationPath": "Project\\2026\\Sprint 20",
            "System.AssignedTo": {"displayName": "Developer", "uniqueName": "dev@example.test"},
            "System.Description": "<p>Existing description.</p>",
            "Microsoft.VSTS.Common.AcceptanceCriteria": "<p>Existing criteria.</p>",
            "Microsoft.VSTS.Scheduling.StoryPoints": 5 if kind == "User Story" else 0,
            "Microsoft.VSTS.Scheduling.RemainingWork": 2,
            "System.Tags": "blocked" if work_item_id == 3 else "",
        },
    )


class NativeServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.items = {
            1: item(1, "Parent story"),
            2: item(2, "Closed task", "Task", "Closed"),
            3: item(3, "[KL STORE] Fix duplicated subscriptions", "Technical Debt"),
            4: item(4, "Investigate API", "Spike"),
        }
        self.items[1].relations = [SimpleNamespace(rel="System.LinkTypes.Hierarchy-Forward", url="https://example.test/wit/2")]
        self.wit = MagicMock()
        self.wit.get_work_item.side_effect = self.get_item
        self.wit.get_work_items.side_effect = lambda ids, fields=None: [copy.deepcopy(self.items[int(value)]) for value in ids]
        self.wit.query_by_wiql.side_effect = self.query
        self.wit.get_work_item_types.return_value = [SimpleNamespace(name=name) for name in ("Task", "User Story", "Technical Debt", "Bug", "Spike")]
        self.wit.get_work_item_type_fields_with_references.return_value = [
            SimpleNamespace(reference_name=name) for name in (*services.WORK_ITEM_FIELD_ALIASES, "System.State", "System.AssignedTo", "System.AreaPath")
        ]
        self.wit.create_work_item.side_effect = self.create_item
        self.wit.update_work_item.side_effect = self.update_item
        self.work = MagicMock()
        attributes = SimpleNamespace(start_date=None, finish_date=None, time_frame="current")
        self.work.get_team_iterations.return_value = [SimpleNamespace(name="Sprint 20", path="Project\\2026\\Sprint 20", attributes=attributes)]
        year = datetime.now().year
        self.wit.get_classification_node.return_value = SimpleNamespace(children=[SimpleNamespace(name=str(year), children=[
            SimpleNamespace(name="Sprint 21 - Sustentacao", path=f"\\Project\\Iteration\\{year}\\Sprint 21 - Sustentacao", attributes=attributes),
            SimpleNamespace(name="Sprint 21", path=f"\\Project\\Iteration\\{year}\\Sprint 21", attributes=attributes),
            SimpleNamespace(name="Sprint 25", path=f"\\Project\\Iteration\\{year}\\Sprint 25", attributes=attributes),
        ])])
        self.git = MagicMock()
        self.repository = SimpleNamespace(name="KL Store", id="repo-id", project=SimpleNamespace(id="project-id"))
        self.git.get_repositories.return_value = [self.repository]
        self.prs: list[Any] = []
        self.git.get_pull_requests.side_effect = lambda **kwargs: list(self.prs)
        self.git.create_pull_request.side_effect = self.create_pr
        self.git.get_pull_request_work_item_refs.return_value = [SimpleNamespace(id="1", url="https://example.test/wit/1")]
        self.connection = MagicMock()
        self.connection.clients.get_work_item_tracking_client.return_value = self.wit
        self.connection.clients.get_work_client.return_value = self.work
        self.connection.clients.get_git_client.return_value = self.git
        self.connection.clients.get_core_client.return_value.get_teams.return_value = []
        patches = [
            patch.dict(os.environ, {
                "AZURE_ORG_URL": "https://example.test", "AZURE_PROJECT": "Project", "AZURE_TEAM": "Main Team",
                "AZURE_PAT": "test-only", "AZURE_USER_EMAIL": "dev@example.test", "AZURE_USER_NAME": "Developer",
                "KWIKLEDGERS_MCP_SERVERS_PATH": str(self.root / "missing"),
            }, clear=True),
            patch.object(services, "get_client", return_value=self.connection),
            patch.object(services, "load_configuration", return_value=None),
            patch.object(services, "urlopen", side_effect=OSError("simulated unavailable REST goal")),
            patch.object(services, "WORKSPACE_ROOT", self.root),
            patch.object(services, "TECHNICAL_DEBT_CONTEXT_DIR", self.root / "debts"),
            patch.object(services, "TECHNICAL_DEBT_PREVIEW_CACHE_DIR", self.root / "previews"),
            patch("kwikledgers_fastmcp.domains.azure_devops.configuration.load_configuration"),
            patch("kwikledgers_fastmcp.domains.azure_devops.native.load_configuration"),
            patch("kwikledgers_fastmcp.domains.azure_devops.read_services.load_dotenv"),
            patch("socket.socket.connect", side_effect=AssertionError("Live network forbidden in native tests")),
        ]
        for replacement in patches:
            replacement.start()
            self.addCleanup(replacement.stop)
        services.WORK_ITEM_TYPE_CACHE.clear()
        services.WORK_ITEM_FIELD_CACHE.clear()
        services.TEAM_CONTEXT_CACHE.clear()

    def get_item(self, id: int, **kwargs: Any) -> Any:
        return copy.deepcopy(self.items[int(id)])

    def query(self, query: Any, **kwargs: Any) -> Any:
        if "IN ('Technical Debt'" in query.query:
            ids = [3]
        elif "[System.WorkItemType] = 'User Story'" in query.query:
            ids = [1]
        else:
            ids = list(self.items)
        return SimpleNamespace(work_items=[SimpleNamespace(id=value) for value in ids])

    def create_item(self, document: list[dict[str, Any]], project: str, type: str) -> Any:
        created = item(max(self.items) + 1, "", type)
        created.rev = 1
        self.items[created.id] = created
        return self.update_item(document=document, id=created.id)

    def update_item(self, document: list[dict[str, Any]], id: int) -> Any:
        current = self.items[int(id)]
        for operation in document:
            if operation["op"] == "test" and operation["path"] == "/rev":
                if operation["value"] != current.rev:
                    raise RuntimeError("Azure revision conflict")
            elif operation["path"].startswith("/fields/"):
                current.fields[operation["path"][len("/fields/"):]] = operation["value"]
            elif operation["path"] == "/relations/-":
                current.relations.append(SimpleNamespace(**operation["value"]))
        current.rev += 1
        return copy.deepcopy(current)

    def create_pr(self, request: Any, **kwargs: Any) -> Any:
        created = SimpleNamespace(
            pull_request_id=99, title=request.title, source_ref_name=request.source_ref_name,
            target_ref_name=request.target_ref_name, is_draft=request.is_draft,
            artifact_id=None, remote_url="https://example.test/pr/99",
            creation_date=datetime.now(timezone.utc), created_by=SimpleNamespace(unique_name="dev@example.test"),
        )
        self.prs.append(created)
        return created

    def invoke(self, name: str, **arguments: Any) -> Any:
        return asyncio.run(invoke_azure_capability(name, arguments))["data"]

    def branch_request(self, **extra: Any) -> dict[str, Any]:
        return {"story_id": 1, "repository_name": "kl_store", "source_branch": "feature/KL-1-story", **extra}

    def pr_request(self, **extra: Any) -> dict[str, Any]:
        return {
            **self.branch_request(), "what_was_changed": "Changes", "affected_processes": "Subscriptions",
            "expected_impacts": "No duplicates", "important_points": "SDK native", "mermaid_diagram": "",
            **extra,
        }

    def test_get_active_user(self) -> None:
        self.assertEqual(self.invoke("get_active_user"), {"message": "Usuario ativo: Developer <dev@example.test>"})
        self.wit.update_work_item.assert_not_called()

    def test_daily_summary_preserves_all_types_closed_tasks_and_children(self) -> None:
        data = self.invoke("get_my_daily_summary")
        self.assertEqual(data["counts"]["assigned_items_by_type"], {"Spike": 1, "Task": 1, "Technical Debt": 1, "User Story": 1})
        self.assertEqual(data["counts"]["tasks_by_state"], {"Closed": 1})
        self.assertEqual(data["child_items_summary"]["completed_children"], 1)
        self.assertEqual(data["remaining_story_points"], 5)
        self.assertFalse(data["sprint_context"]["goal_available"])
        self.assertEqual(data["blocked_items"][0]["id"], 3)
        self.wit.update_work_item.assert_not_called()

    def test_get_my_work_items_removes_only_open_pr_list(self) -> None:
        data = self.invoke("get_my_work_items")
        self.assertNotIn("open_prs", data)
        self.assertIn("open_prs", data["counts"])
        self.assertEqual(len(data["items"]), 4)

    def test_get_my_blocked_items_payload(self) -> None:
        data = self.invoke("get_my_blocked_items")
        self.assertEqual(set(data), {"generated_at", "project", "user_email", "blocked_count", "blocked_items"})
        self.assertEqual(data["blocked_count"], 1)

    def test_get_user_stories_text_payload(self) -> None:
        data = self.invoke("get_user_stories", email="dev@example.test")
        self.assertIn("KL-1: Parent story", data["message"])
        self.assertIn("Story Points: 5", data["message"])

    def test_get_sprint_stories_light_payload(self) -> None:
        data = self.invoke("get_sprint_stories")
        self.assertEqual(data["detail_mode"], "light")
        self.assertNotIn("description", data["sprint_stories"][0])
        self.assertEqual(data["project_progress"]["active_stories"], 1)

    def test_get_sprint_stories_detailed_payload(self) -> None:
        data = self.invoke("get_sprint_stories_detailed")
        self.assertEqual(data["detail_mode"], "detailed")
        self.assertEqual(data["sprint_stories"][0]["description"], "Existing description.")
        self.assertEqual(data["sprint_stories"][0]["acceptance_criteria"], "Existing criteria.")
        self.assertIn("Criterio-chave", data["sprint_stories"][0]["summary"])

    def test_get_story_details_includes_child_tasks(self) -> None:
        data = self.invoke("get_story_details", story_id=1)
        self.assertIn("=== KL-1: Parent story ===", data["message"])
        self.assertIn("[Closed] Closed task", data["message"])
        self.assertEqual((data["id"], data["revision"], data["current_status"], data["work_item_type"]), (1, 7, "Active", "User Story"))
        self.assertEqual(data["child_work_items"][0]["revision"], 7)
        self.assertEqual(data["child_work_items"][0]["work_item_type"], "Task")

    def test_get_open_prs_filters_creator_and_handles_timezone(self) -> None:
        self.create_pr(SimpleNamespace(title="PR", source_ref_name="refs/heads/source", target_ref_name="refs/heads/target", is_draft=True))
        self.assertIn("PR #99: PR", self.invoke("get_open_prs", email="dev@example.test")["message"])
        self.assertEqual(self.invoke("get_open_prs", email="other@example.test")["message"], "Nenhuma PR aberta encontrada para este usuario.")

    def test_preview_story_branch_association_is_read_only(self) -> None:
        data = self.invoke("preview_story_branch_association", **self.branch_request(related_work_item_ids=[2, 4, 4]))
        self.assertEqual(data["counts"], {"total_work_items": 3, "already_linked": 0, "pending_link": 3})
        self.assertEqual(data["branch_artifact_uri"], "vstfs:///Git/Ref/project-id%2frepo-id%2fGBfeature%2fKL-1-story")
        self.assertEqual([entry["relationship_to_story"] for entry in data["work_items"]], ["parent", "child", "related"])
        self.wit.update_work_item.assert_not_called()

    def test_preview_story_pull_request_has_no_governance_side_effect(self) -> None:
        data = self.invoke("preview_story_pull_request", **self.pr_request(target_branch="refs/heads/custom-stage"))
        self.assertEqual(data["target_branch"], "custom-stage")
        self.assertTrue(data["is_draft"])
        self.assertTrue(data["can_open_pull_request"])
        self.assertNotIn("recommended_story_status_transition", data)
        self.assertEqual(data["template"]["source"], "default")
        self.wit.update_work_item.assert_not_called()
        self.git.create_pull_request.assert_not_called()

    def test_associate_story_branch_is_idempotent_and_revision_guarded(self) -> None:
        first = self.invoke("associate_story_branch", **self.branch_request())
        self.assertEqual(first["association_result"]["associated_count"], 2)
        for invocation in self.wit.update_work_item.call_args_list:
            self.assertEqual(invocation.kwargs["document"][0], {"op": "test", "path": "/rev", "value": 7})
        second = self.invoke("associate_story_branch", **self.branch_request())
        self.assertEqual(second["association_result"]["already_linked_count"], 2)
        self.assertEqual(self.wit.update_work_item.call_count, 2)

    def test_create_story_pull_request_reuses_pr_and_preserves_work_item_links(self) -> None:
        first = self.invoke("create_story_pull_request", **self.pr_request())
        self.assertTrue(first["created"])
        self.assertIn("/pullrequest/99", first["pull_request"]["web_url"])
        self.assertEqual(first["pull_request_association_result"]["associated_count"], 2)
        self.assertEqual(self.items[1].fields["System.State"], "Active")
        second = self.invoke("create_story_pull_request", **self.pr_request())
        self.assertFalse(second["created"])
        self.assertEqual(second["pull_request_association_result"]["already_linked_count"], 2)
        self.git.create_pull_request.assert_called_once()

    def test_pr_revision_map_advances_only_after_successful_writes(self) -> None:
        result = self.invoke("create_story_pull_request", **self.pr_request(expected_revisions={"1": 7, "2": 7}))
        self.assertTrue(result["created"])
        revisions = [call.kwargs["document"][0]["value"] for call in self.wit.update_work_item.call_args_list]
        self.assertEqual(revisions, [7, 7, 8, 8])

    def test_branch_preview_exposes_revision_for_every_target(self) -> None:
        result = self.invoke("preview_story_branch_association", **self.branch_request())
        self.assertEqual(result["parent_work_item"]["revision"], 7)
        self.assertEqual([entry["revision"] for entry in result["work_items"]], [7, 7])

    def test_large_work_item_query_is_batched(self) -> None:
        self.items = {value: item(value, f"Story {value}") for value in range(1, 211)}
        data = services._query_assigned_items("dev@example.test")
        self.assertEqual(len(data), 210)
        self.assertEqual([len(call.kwargs["ids"]) for call in self.wit.get_work_items.call_args_list], [75, 75, 60])

    def test_wiql_email_quotes_are_escaped(self) -> None:
        self.invoke("get_user_stories", email="o'connor@example.test")
        query = self.wit.query_by_wiql.call_args.args[0].query
        self.assertIn("o''connor@example.test", query)

    def test_create_work_item_resolves_alias_inherits_iteration_and_formats_html(self) -> None:
        data = self.invoke("create_work_item", work_item_type="task", title="Child", parent_work_item_id=1,
                           description="# Heading\n\n- Check `value`", remaining_work_hours=0)
        self.assertEqual(data["resolved_work_item_type"], "Task")
        self.assertEqual(data["resolved_iteration_path"], "Project\\2026\\Sprint 20")
        self.assertEqual(data["requested_effort"], {"remaining_work_hours": 0})
        document = self.wit.create_work_item.call_args.kwargs["document"]
        fields = {operation["path"]: operation["value"] for operation in document}
        self.assertIn("<ul><li>Check <code>value</code></li></ul>", fields["/fields/System.Description"])
        self.assertEqual(fields["/relations/-"]["rel"], "System.LinkTypes.Hierarchy-Reverse")

    def test_create_technical_debt_preserves_local_context_side_effect(self) -> None:
        data = self.invoke("create_work_item", work_item_type="debito tecnico", title="[KL STORE] Fix duplicated subscriptions")
        self.assertEqual(data["repository_name"], "kl_store")
        self.assertTrue(Path(data["local_context_path"]).is_file())
        self.assertTrue(data["similarity_analysis"]["duplicate_found"])

    def test_find_similar_technical_debts_and_cache_payload(self) -> None:
        data = self.invoke("find_similar_technical_debts", repository_name="kl_store", title="[KL STORE] Fix duplicated subscriptions", limit=1)
        self.assertTrue(data["duplicate_found"])
        self.assertEqual(data["suggested_matches"][0]["id"], 3)
        self.assertTrue(Path(data["local_context_path"]).is_file())
        self.wit.update_work_item.assert_not_called()

    def test_save_technical_debt_preview_cache_merges_title_and_validates_children(self) -> None:
        request = {"repository_name": "kl_store", "title": "Preview", "implementation_status": "already implemented", "child_tasks": [{"title": "Child", "remaining_work_hours": 0}]}
        first = self.invoke("save_technical_debt_preview_cache", **request)
        self.assertTrue(first["saved"])
        self.assertEqual(first["implementation_status"], "ja_implementado")
        self.invoke("save_technical_debt_preview_cache", **request)
        saved = json.loads(Path(first["cache_path"]).read_text(encoding="utf-8"))
        self.assertEqual(len(saved["preview_entries"]), 1)
        self.assertEqual(saved["preview_entries"][0]["child_tasks"][0]["remaining_work_hours"], 0)
        self.wit.update_work_item.assert_not_called()

    def test_update_work_item_content_preserves_html_and_revision(self) -> None:
        data = self.invoke("update_work_item_content", work_item_id=1, description="1. First 2. Second", acceptance_criteria="- Accepted", comment="Reviewed", expected_revision=7)
        self.assertEqual(data["applied_fields"], ["description", "acceptance_criteria"])
        self.assertEqual(data["description_format"], "azure_simple_html")
        self.assertEqual(data["revision"], 8)
        self.assertIn("<ol><li>First</li><li>Second</li></ol>", self.items[1].fields["System.Description"])

    def test_update_work_item_effort_preserves_empty_remaining_and_support_filter(self) -> None:
        self.wit.get_work_item_type_fields_with_references.return_value = [SimpleNamespace(reference_name="Microsoft.VSTS.Scheduling.CompletedWork")]
        data = self.invoke("update_work_item_effort", work_item_id=2, completed_work_hours=3, story_points=8, expected_revision=7)
        self.assertEqual(data["applied_fields"], ["completed_work_hours"])
        self.assertEqual(data["skipped_fields"], ["story_points"])
        document = self.wit.update_work_item.call_args.kwargs["document"]
        self.assertFalse(any(operation["path"].endswith("RemainingWork") for operation in document))
        self.assertEqual(self.items[2].fields["Microsoft.VSTS.Scheduling.RemainingWork"], 2)

    def test_move_work_item_to_next_sprint_uses_current_plus_one_and_main_variant(self) -> None:
        data = self.invoke("move_work_item_to_next_sprint", work_item_id=1, comment="Move", expected_revision=7)
        self.assertTrue(data["moved"])
        self.assertTrue(data["resolved_iteration_path"].endswith("\\Sprint 21"))
        self.assertEqual(data["next_sprint_context"]["resolution_strategy"], "current_sprint_plus_one")
        self.assertFalse(self.invoke("move_work_item_to_next_sprint", work_item_id=1)["moved"])
        self.wit.update_work_item.assert_called_once()

    def test_update_story_status_is_a_technical_patch_not_a_workflow(self) -> None:
        data = self.invoke("update_story_status", story_id=2, status="Custom process state", expected_revision=7)
        self.assertEqual(data["message"], "Historia KL-2 atualizada para: Custom process state")
        self.assertEqual(self.items[2].fields["System.State"], "Custom process state")
        self.assertEqual((data["revision"], data["current_status"]), (8, "Custom process state"))
        self.assertTrue(data["status_confirmed"])

    def test_status_mismatch_is_not_reported_as_success(self) -> None:
        self.wit.update_work_item.side_effect = lambda **kwargs: self.get_item(kwargs["id"])
        for name, arguments in (
            ("update_story_status", {"story_id": 1, "status": "Closed"}),
            ("update_work_item_content", {"work_item_id": 1, "status": "Closed"}),
            ("update_work_item_effort", {"work_item_id": 1, "completed_work_hours": 3, "status": "Closed"}),
        ):
            with self.subTest(name=name), self.assertRaisesRegex(AzureCapabilityError, "status mismatch"):
                self.invoke(name, **arguments, expected_revision=7)

    def test_task_closure_is_one_patch_and_preserves_remaining_work(self) -> None:
        data = self.invoke("update_work_item_effort", work_item_id=1, completed_work_hours=3, status="Closed", expected_revision=7)
        self.assertEqual((data["revision"], data["current_status"], data["completed_work_hours"]), (8, "Closed", 3))
        self.wit.update_work_item.assert_called_once()
        self.assertEqual(self.wit.update_work_item.call_args.kwargs["document"], [
            {"op": "test", "path": "/rev", "value": 7},
            {"op": "add", "path": "/fields/Microsoft.VSTS.Scheduling.CompletedWork", "value": 3},
            {"op": "add", "path": "/fields/System.State", "value": "Closed"},
        ])
        self.assertEqual(self.items[1].fields["Microsoft.VSTS.Scheduling.RemainingWork"], 2)

    def test_story_start_assignment_area_and_status_are_one_patch(self) -> None:
        data = self.invoke("update_work_item_content", work_item_id=2, assigned_to="dev@example.test", area_path="Project\\Team", status="Active", expected_revision=7)
        self.assertEqual(data["applied_fields"], ["assigned_to", "area_path", "status"])
        self.assertEqual((data["revision"], data["current_status"], data["area_path"]), (8, "Active", "Project\\Team"))
        self.wit.update_work_item.assert_called_once()
        self.assertEqual([entry["path"] for entry in self.wit.update_work_item.call_args.kwargs["document"]], [
            "/rev", "/fields/System.AssignedTo", "/fields/System.AreaPath", "/fields/System.State",
        ])
        self.assertEqual(self.items[2].fields["System.AssignedTo"], "dev@example.test")

    def test_atomic_closure_rejects_unsupported_effort_without_closing(self) -> None:
        self.wit.get_work_item_type_fields_with_references.return_value = [SimpleNamespace(reference_name="System.State")]
        with self.assertRaisesRegex(AzureCapabilityError, "No requested effort"):
            self.invoke("update_work_item_effort", work_item_id=1, completed_work_hours=3, status="Closed", expected_revision=7)
        self.wit.update_work_item.assert_not_called()

    def test_add_story_comment_retains_text_and_revision_guard(self) -> None:
        data = self.invoke("add_story_comment", story_id=1, comment="Comment", expected_revision=7)
        self.assertEqual(data["message"], "Comentario adicionado na historia KL-1")
        self.assertEqual(self.items[1].fields["System.History"], "Comment")

    def test_stale_revision_fails_without_automatic_retry(self) -> None:
        with self.assertRaisesRegex(AzureCapabilityError, "revision conflict"):
            self.invoke("update_story_status", story_id=1, status="Closed", expected_revision=6)
        self.assertEqual(self.items[1].fields["System.State"], "Active")
        self.wit.update_work_item.assert_called_once()

    def test_revision_map_guards_each_association_target(self) -> None:
        with self.assertRaisesRegex(AzureCapabilityError, "revision conflict"):
            self.invoke("associate_story_branch", **self.branch_request(expected_revisions={"1": 7, "2": 6}))
        self.assertEqual(len(self.items[1].relations), 2)
        self.assertEqual(len(self.items[2].relations), 0)

    def test_revision_reader_returns_sdk_revision(self) -> None:
        self.assertEqual(work_item_revision(self.wit, 1), 7)

    def test_invalid_revision_is_rejected_before_azure_access(self) -> None:
        for value in (True, 0, -1, "7", 7.0):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.invoke("update_story_status", story_id=1, status="Closed", expected_revision=value)
        self.wit.update_work_item.assert_not_called()

    def test_empty_content_and_effort_do_not_write(self) -> None:
        for name in ("update_work_item_content", "update_work_item_effort"):
            with self.subTest(name=name):
                with self.assertRaises(AzureCapabilityError):
                    self.invoke(name, work_item_id=1)
        self.wit.update_work_item.assert_not_called()

    def test_empty_user_and_story_queries_keep_legacy_messages(self) -> None:
        with patch.object(services, "_resolve_active_user_email", return_value=None):
            self.assertIn("Email nao configurado", self.invoke("get_my_daily_summary")["message"])
        self.wit.query_by_wiql.return_value = SimpleNamespace(work_items=[])
        self.wit.query_by_wiql.side_effect = None
        self.assertEqual(self.invoke("get_user_stories", email="dev@example.test")["message"], "Nenhuma historia encontrada para este usuario.")

    def test_cache_keys_cannot_escape_tracking_directory(self) -> None:
        result = self.invoke("save_technical_debt_preview_cache", repository_name="../../outside", title="Preview")
        self.assertEqual(Path(result["cache_path"]).parent, self.root / "previews")
        with self.assertRaises(AzureCapabilityError):
            self.invoke("save_technical_debt_preview_cache", repository_name="../..", title="Preview")

    def test_pr_preview_reads_repository_template_context(self) -> None:
        repository = self.root / "projects" / "kl_store"
        repository.mkdir(parents=True)
        template = repository / "pull_request_template.md"
        template.write_text("# O que foi modificado\n", encoding="utf-8")
        data = self.invoke("preview_story_pull_request", **self.pr_request())
        self.assertEqual(data["template"], {"found": True, "path": str(template), "source": "repository"})

    def test_nonempty_branch_is_required_before_mutation(self) -> None:
        with self.assertRaises(AzureCapabilityError):
            self.invoke("associate_story_branch", **self.branch_request(source_branch="refs/heads/"))
        self.wit.update_work_item.assert_not_called()

    def test_all_21_payloads_match_original_functions_with_documented_deltas(self) -> None:
        original_path = Path(__file__).resolve().parents[2] / "agent/mcp_servers/azure_devops/server.py"
        if not original_path.is_file():
            self.skipTest("Original source is needed only for offline parity validation.")
        tree = ast.parse(original_path.read_text(encoding="utf-8-sig"))
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name not in {"get_client", "get_project"}]
        for function in functions:
            function.decorator_list = []
        reference = dict(vars(services))
        reference.update({
            "get_client": lambda: self.connection,
            "LOADED_ENV_FILE": None,
            "ALLOWED_PULL_REQUEST_TARGET_BRANCHES": ("stage-pre-prod", "stage-homolog"),
            "WORK_ITEM_TYPE_CACHE": {}, "WORK_ITEM_FIELD_CACHE": {}, "TEAM_CONTEXT_CACHE": {},
        })
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(original_path), "exec"), reference)

        class FrozenDateTime(datetime):
            @classmethod
            def utcnow(cls) -> datetime:
                return datetime(2026, 9, 29, 12)

            @classmethod
            def now(cls, tz: Any = None) -> datetime:
                return datetime(2026, 9, 29, 12, tzinfo=tz)

        reference["datetime"] = FrozenDateTime
        requests = {
            "get_active_user": {}, "get_my_work_items": {}, "get_my_blocked_items": {}, "get_my_daily_summary": {},
            "get_user_stories": {"email": "dev@example.test"}, "get_sprint_stories": {}, "get_sprint_stories_detailed": {},
            "get_story_details": {"story_id": 1}, "get_open_prs": {"email": "dev@example.test"},
            "preview_story_branch_association": self.branch_request(), "associate_story_branch": self.branch_request(),
            "preview_story_pull_request": self.pr_request(mermaid_diagram="diagram-fixture"),
            "create_story_pull_request": self.pr_request(mermaid_diagram="diagram-fixture"),
            "create_work_item": {"work_item_type": "Task", "title": "Child", "parent_work_item_id": 1, "description": "- Item", "completed_work_hours": 0},
            "find_similar_technical_debts": {"repository_name": "kl_store", "title": "[KL STORE] Fix duplicated subscriptions"},
            "save_technical_debt_preview_cache": {"repository_name": "kl_store", "title": "Preview", "child_tasks": [{"title": "Child"}]},
            "update_work_item_content": {"work_item_id": 1, "description": "Updated", "acceptance_criteria": "1. Accepted"},
            "update_work_item_effort": {"work_item_id": 2, "completed_work_hours": 3},
            "move_work_item_to_next_sprint": {"work_item_id": 1},
            "update_story_status": {"story_id": 1, "status": "Closed"}, "add_story_comment": {"story_id": 1, "comment": "Comment"},
        }

        def normalize(value: Any) -> Any:
            if isinstance(value, str):
                try:
                    return normalize(json.loads(value))
                except json.JSONDecodeError:
                    return {"message": value}
            if isinstance(value, dict):
                return {key: normalize(child) if isinstance(child, (dict, list)) else child
                        for key, child in value.items() if key not in {"revision", "current_status", "area_path", "recommended_story_status_transition"}}
            if isinstance(value, list):
                return [normalize(child) for child in value]
            return value

        seed_items = copy.deepcopy(self.items)
        with patch.object(services, "datetime", FrozenDateTime):
            for name, arguments in requests.items():
                with self.subTest(capability=name):
                    self.items = copy.deepcopy(seed_items)
                    self.prs.clear()
                    expected = reference[f"_{name}"](**arguments)
                    self.items = copy.deepcopy(seed_items)
                    self.prs.clear()
                    actual = self.invoke(name, **arguments)
                    if name in {"get_story_details", "update_story_status"}:
                        actual = {"message": actual["message"]}
                    self.assertEqual(normalize(actual), normalize(expected))