from __future__ import annotations

import os
import json
import logging
import subprocess
import re
import unicodedata
from difflib import SequenceMatcher
from html import escape, unescape
from html.parser import HTMLParser
from base64 import b64encode
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote
from urllib.request import Request, urlopen

from azure.devops.v7_0.git.git_client import GitClient
from azure.devops.v7_0.git.models import GitPullRequest, GitPullRequestSearchCriteria, GitRepository
from azure.devops.v7_0.work.models import TeamSettingsIteration
from azure.devops.v7_0.work_item_tracking.models import (
    IdentityRef, TeamContext, Wiql, WorkItem, WorkItemClassificationNode,
    WorkItemField, WorkItemRelation, WorkItemTypeFieldWithReferences,
)
from azure.devops.v7_0.work_item_tracking.work_item_tracking_client import WorkItemTrackingClient

from .configuration import get_client, get_project, load_configuration, workspace_root
from .primitives import EXPECTED_REVISIONS, update_work_item, validate_revision


DEFAULT_PULL_REQUEST_TARGET_BRANCH = "stage-pre-prod"
COMPLETED_STATES = {"done", "closed", "resolved"}
WORK_ITEM_TYPE_ALIASES = {
    "task": ("Task",),
    "tasks": ("Task",),
    "story": ("User Story", "Story"),
    "stories": ("User Story", "Story"),
    "user story": ("User Story", "Story"),
    "user stories": ("User Story", "Story"),
    "historia": ("User Story", "Story"),
    "historias": ("User Story", "Story"),
    "bug": ("Bug",),
    "bugs": ("Bug",),
    "technical debt": ("Technical Debt", "Technical debt", "Tech Debt"),
    "technical debts": ("Technical Debt", "Technical debt", "Tech Debt"),
    "tech debt": ("Technical Debt", "Technical debt", "Tech Debt"),
    "tech debts": ("Technical Debt", "Technical debt", "Tech Debt"),
    "debito tecnico": ("Technical Debt", "Technical debt", "Tech Debt"),
    "debitos tecnicos": ("Technical Debt", "Technical debt", "Tech Debt"),
}
WORK_ITEM_FIELD_ALIASES = {
    "Microsoft.VSTS.Common.AcceptanceCriteria": "acceptance_criteria",
    "Microsoft.VSTS.Scheduling.StoryPoints": "story_points",
    "Microsoft.VSTS.Scheduling.RemainingWork": "remaining_work_hours",
    "Microsoft.VSTS.Scheduling.CompletedWork": "completed_work_hours",
    "Microsoft.VSTS.Scheduling.OriginalEstimate": "original_estimate_hours",
}
WORK_ITEM_TYPE_CACHE: dict[str, dict[str, str]] = {}
WORK_ITEM_FIELD_CACHE: dict[tuple[str, str], set[str]] = {}
TEAM_CONTEXT_CACHE: dict[str, str] = {}
WORKSPACE_ROOT = workspace_root()
TECHNICAL_DEBT_CONTEXT_DIR = WORKSPACE_ROOT / "AI_Tracking" / "Repo_Work_Item_Context" / "technical_debts"
TECHNICAL_DEBT_PREVIEW_CACHE_DIR = WORKSPACE_ROOT / "AI_Tracking" / "Repo_Work_Item_Context" / "technical_debt_previews"
SIMILARITY_STOPWORDS = {
    "a", "ao", "aos", "as", "bug", "bugs", "com", "como", "da", "das", "de", "debito",
    "debt", "do", "dos", "e", "em", "for", "generic", "historia", "issue", "it", "its",
    "na", "nas", "no", "nos", "o", "of", "os", "ou", "para", "por", "repository", "repo",
    "service", "story", "task", "tasks", "tecnica", "tecnico", "technical", "tecnicas", "tecnicos",
    "the", "to", "um", "uma", "update", "work", "item",
}
ACTIVE_DAILY_SUMMARY_EXCLUDED_STATES = ("Closed", "Removed", "Done", "Resolved")

def get_team_context() -> TeamContext:
    team_name = _resolve_team_name()
    if team_name:
        return TeamContext(project=get_project(), team=team_name)
    return TeamContext(project=get_project())


def get_active_pr_search_criteria() -> GitPullRequestSearchCriteria:
    return GitPullRequestSearchCriteria(status="active")


def get_age_days(created_at: Optional[datetime]) -> int:
    if created_at is None:
        return 0

    if created_at.tzinfo is not None:
        return (datetime.now(created_at.tzinfo) - created_at).days

    return (datetime.utcnow() - created_at).days

def _resolve_active_user_email() -> Optional[str]:
    configured_email = os.environ.get("AZURE_USER_EMAIL", "").strip()
    if configured_email:
        return configured_email

    for command in (["git", "config", "user.email"], ["git", "config", "--global", "user.email"]):
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=5)
        except Exception:
            continue

        email = result.stdout.strip()
        if email:
            return email

    return None


def _resolve_active_user_identity() -> tuple[Optional[str], Optional[str], Optional[str]]:
    configured_email = os.environ.get("AZURE_USER_EMAIL", "").strip()
    if configured_email:
        env_file = str(env_path) if (env_path := load_configuration()) else None
        return configured_email, "AZURE_USER_EMAIL", env_file

    for command in ((["git", "config", "user.email"], "git config user.email"), (["git", "config", "--global", "user.email"], "git config --global user.email")):
        try:
            result = subprocess.run(command[0], capture_output=True, text=True, timeout=5)
        except Exception:
            continue

        email = result.stdout.strip()
        if email:
            return email, command[1], None

    return None, None, None


def _resolve_active_user_name(email: Optional[str]) -> Optional[str]:
    configured_name = os.environ.get("AZURE_USER_NAME", "").strip()
    if configured_name:
        return configured_name

    for command in ("git config user.name", "git config --global user.name"):
        try:
            result = subprocess.run(command.split(), capture_output=True, text=True, timeout=5)
        except Exception:
            continue

        name = result.stdout.strip()
        if name:
            return name

    local_part = str(email or "").split("@", 1)[0].strip()
    if not local_part:
        return None

    normalized = re.sub(r"[._-]+", " ", local_part)
    cleaned = " ".join(chunk for chunk in normalized.split() if chunk)
    if not cleaned:
        return None

    return cleaned.title()


def _format_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=True)


def _normalize_whitespace(value: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", value)).strip()


def _strip_html(value: str | None) -> str:
    if not value:
        return ""

    cleaned = unescape(value)
    replacements = {
        "<br>": "\n",
        "<br/>": "\n",
        "<br />": "\n",
        "</p>": "\n\n",
        "</div>": "\n",
        "</li>": "\n",
        "<li>": "- ",
        "</ul>": "\n",
        "</ol>": "\n",
    }
    for source, target in replacements.items():
        cleaned = cleaned.replace(source, target)
        cleaned = cleaned.replace(source.upper(), target)

    cleaned = re.sub(r"<[^>]+>", "", cleaned)
    return _normalize_whitespace(cleaned)


def _split_inline_numbered_items(line: str) -> list[str]:
    stripped_line = str(line or "").strip()
    if not stripped_line:
        return []

    if not re.search(r"(?:^|\s)1\.\s", stripped_line):
        return [stripped_line]

    numbered_matches = list(re.finditer(r"(?:^|\s)(\d+)\.\s+", stripped_line))
    if len(numbered_matches) < 2:
        return [stripped_line]

    items: list[str] = []
    for index, match in enumerate(numbered_matches):
        content_start = match.end()
        content_end = numbered_matches[index + 1].start() if index + 1 < len(numbered_matches) else len(stripped_line)
        content = stripped_line[content_start:content_end].strip(" ;")
        if content:
            items.append(f"{match.group(1)}. {content}")

    return items or [stripped_line]


def _apply_inline_code_markup(value: str) -> str:
    return re.sub(r"`([^`]+)`", lambda match: f"<code>{escape(match.group(1), quote=False)}</code>", value)


def _normalize_markdown_lines(value: str | None) -> list[str]:
    if not value:
        return []

    normalized = str(value).replace("\r\n", "\n").replace("\r", "\n")
    expanded_lines: list[str] = []
    inside_code_fence = False
    for raw_line in normalized.split("\n"):
        stripped_line = raw_line.strip()
        if stripped_line.startswith("```"):
            expanded_lines.append(raw_line)
            inside_code_fence = not inside_code_fence
            continue

        if inside_code_fence:
            expanded_lines.append(raw_line)
            continue

        split_lines = _split_inline_numbered_items(raw_line)
        expanded_lines.extend(split_lines or [raw_line])
    return expanded_lines


def _render_lines_as_azure_html(lines: list[str]) -> str:
    html_parts: list[str] = []
    paragraph_lines: list[str] = []
    list_type: str | None = None
    list_items: list[str] = []
    code_language: str | None = None
    code_lines: list[str] = []

    def flush_paragraph() -> None:
        nonlocal paragraph_lines
        if not paragraph_lines:
            return
        paragraph = " ".join(part.strip() for part in paragraph_lines if part.strip())
        paragraph_lines = []
        if paragraph:
            html_parts.append(f"<p>{_apply_inline_code_markup(escape(paragraph, quote=False))}</p>")

    def flush_list() -> None:
        nonlocal list_type, list_items
        if not list_type or not list_items:
            list_type = None
            list_items = []
            return
        items_html = "".join(
            f"<li>{_apply_inline_code_markup(escape(item.strip(), quote=False))}</li>" for item in list_items if item.strip()
        )
        if items_html:
            html_parts.append(f"<{list_type}>{items_html}</{list_type}>")
        list_type = None
        list_items = []

    def flush_code_block() -> None:
        nonlocal code_language, code_lines
        if code_language is None:
            return
        code_content = "\n".join(code_lines)
        escaped_code = escape(code_content, quote=False)
        language_attr = f' class="language-{escape(code_language, quote=True)}"' if code_language else ""
        html_parts.append(f"<pre><code{language_attr}>{escaped_code}</code></pre>")
        code_language = None
        code_lines = []

    for raw_line in lines:
        line = str(raw_line or "").strip()
        fence_match = re.fullmatch(r"```\s*([A-Za-z0-9_+\-]*)\s*", line)

        if code_language is not None:
            if fence_match:
                flush_code_block()
            else:
                code_lines.append(str(raw_line or ""))
            continue

        if fence_match:
            flush_paragraph()
            flush_list()
            code_language = fence_match.group(1).strip() or "text"
            code_lines = []
            continue

        if not line:
            flush_paragraph()
            flush_list()
            continue

        heading_match = re.fullmatch(r"#{1,6}\s*(.+?)\s*", line)
        ordered_match = re.fullmatch(r"(\d+)\.\s+(.+)", line)
        unordered_match = re.fullmatch(r"[-*]\s+(.+)", line)

        if heading_match:
            flush_paragraph()
            flush_list()
            heading_text = heading_match.group(1).strip().rstrip(":")
            html_parts.append(f"<p><strong>{_apply_inline_code_markup(escape(heading_text, quote=False))}</strong></p>")
            continue

        if line.endswith(":") and not ordered_match and not unordered_match:
            flush_paragraph()
            flush_list()
            html_parts.append(f"<p><strong>{_apply_inline_code_markup(escape(line[:-1].strip(), quote=False))}</strong></p>")
            continue

        if ordered_match:
            flush_paragraph()
            if list_type != "ol":
                flush_list()
                list_type = "ol"
            list_items.append(ordered_match.group(2).strip())
            continue

        if unordered_match:
            flush_paragraph()
            if list_type != "ul":
                flush_list()
                list_type = "ul"
            list_items.append(unordered_match.group(1).strip())
            continue

        flush_list()
        paragraph_lines.append(line)

    flush_paragraph()
    flush_list()
    flush_code_block()
    return "".join(html_parts)


class _GeneratedAzureHTMLValidator(HTMLParser):
    def __init__(self, value: str) -> None:
        super().__init__(convert_charrefs=False)
        self.source_lines = value.splitlines(keepends=True)
        self.stack: list[str] = []
        self.valid = True
        self.has_tag = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        inline_tags = {"strong", "em", "code", "br"}
        block_tags = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "pre"}
        if tag not in inline_tags | block_tags | {"li"}:
            self.valid = False
            return
        raw_tag = self.get_starttag_text() or ""
        if attrs:
            if tag != "code" or len(attrs) != 1 or attrs[0][0] != "class":
                self.valid = False
                return
            if re.fullmatch(r"language-[A-Za-z0-9_+\-]+", attrs[0][1] or "") is None:
                self.valid = False
                return
            tag_pattern = r"<code\s+class=(?:\"language-[A-Za-z0-9_+\-]+\"|'language-[A-Za-z0-9_+\-]+')\s*>"
        else:
            tag_pattern = rf"<{tag}\s*{'/?' if tag == 'br' else ''}>"
        if re.fullmatch(tag_pattern, raw_tag) is None:
            self.valid = False
            return
        parent = self.stack[-1] if self.stack else None
        if parent is None:
            allowed = block_tags
        elif parent in {"ul", "ol"}:
            allowed = {"li"}
        elif parent == "li":
            allowed = inline_tags | block_tags
        elif parent == "pre":
            allowed = {"code"}
        elif parent == "code":
            allowed = set()
        else:
            allowed = inline_tags - ({"strong", "em"} & set(self.stack))
        if tag not in allowed:
            self.valid = False
            return
        self.has_tag = True
        if tag != "br":
            self.stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "br":
            self.valid = False
            return
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        line, column = self.getpos()
        remaining = "".join(self.source_lines[line - 1:])[column:]
        if re.match(rf"</{re.escape(tag)}\s*>", remaining) is None:
            self.valid = False
        if not self.stack or self.stack[-1] != tag:
            self.valid = False
            return
        self.stack.pop()

    def handle_data(self, data: str) -> None:
        if "<" in data or ">" in data:
            self.valid = False
        if data.strip() and (not self.stack or self.stack[-1] in {"ul", "ol", "pre"}):
            self.valid = False

    def handle_entityref(self, name: str) -> None:
        self.handle_data(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.handle_data(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        self.valid = False

    def handle_decl(self, decl: str) -> None:
        self.valid = False

    def unknown_decl(self, data: str) -> None:
        self.valid = False

    def handle_pi(self, data: str) -> None:
        self.valid = False


def _format_azure_rich_text(value: str | None) -> str | None:
    if value:
        validator = _GeneratedAzureHTMLValidator(value)
        try:
            validator.feed(value)
            validator.close()
        except (ValueError, AssertionError):
            validator.valid = False
        if validator.valid and validator.has_tag and not validator.stack:
            return value
    lines = _normalize_markdown_lines(value)
    if not lines:
        return None

    html_value = _render_lines_as_azure_html(lines)
    return html_value or None


def _build_browser_work_item_url(work_item_id: int | None) -> str | None:
    if work_item_id is None:
        return None
    org_url = os.environ["AZURE_ORG_URL"].rstrip("/")
    project = quote(get_project(), safe="")
    return f"{org_url}/{project}/_workitems/edit/{work_item_id}"


def _build_pull_request_browser_url(repository_name: str | None, pull_request_id: int | None) -> str | None:
    if pull_request_id is None:
        return None

    normalized_repository_name = str(repository_name or "").strip()
    if not normalized_repository_name:
        return None

    org_url = os.environ["AZURE_ORG_URL"].rstrip("/")
    project = quote(get_project(), safe="")
    repository = quote(normalized_repository_name, safe="")
    return f"{org_url}/{project}/_git/{repository}/pullrequest/{pull_request_id}"


def _build_branch_browser_url(repository_name: str | None, branch_name: str | None) -> str | None:
    normalized_repository_name = str(repository_name or "").strip()
    normalized_branch_name = str(branch_name or "").strip().strip("/")
    if not normalized_repository_name or not normalized_branch_name:
        return None

    org_url = os.environ["AZURE_ORG_URL"].rstrip("/")
    project = quote(get_project(), safe="")
    repository = quote(normalized_repository_name, safe="")
    encoded_version = quote(f"GB{normalized_branch_name}", safe="").replace("%2F", "%2f")
    return f"{org_url}/{project}/_git/{repository}?version={encoded_version}"


def _normalize_source_branch(source_branch: str | None) -> tuple[str, str]:
    normalized_source_branch = str(source_branch or "").strip()
    if not normalized_source_branch:
        raise ValueError("source_branch e obrigatoria para o fluxo de branch/PR da historia.")

    plain_branch_name = normalized_source_branch
    if plain_branch_name.startswith("refs/heads/"):
        plain_branch_name = plain_branch_name[len("refs/heads/"):]
    elif plain_branch_name.startswith("heads/"):
        plain_branch_name = plain_branch_name[len("heads/"):]

    plain_branch_name = plain_branch_name.strip().strip("/")
    if not plain_branch_name:
        raise ValueError("source_branch e obrigatoria para o fluxo de branch/PR da historia.")

    return plain_branch_name, f"refs/heads/{plain_branch_name}"


def _resolve_repository_project_id(repository: GitRepository) -> str:
    project_reference = getattr(repository, "project", None)
    project_id = getattr(project_reference, "id", None)
    if project_id:
        return str(project_id)

    raise ValueError("Nao foi possivel resolver o project id do repositorio no Azure DevOps.")


def _build_branch_artifact_uri(project_id: str, repository_id: str, branch_name: str) -> str:
    encoded_branch = quote(f"GB{branch_name}", safe="").replace("%2F", "%2f")
    return f"vstfs:///Git/Ref/{project_id}%2f{repository_id}%2f{encoded_branch}"


def _build_pull_request_artifact_uri(project_id: str, repository_id: str, pull_request_id: int) -> str:
    return f"vstfs:///Git/PullRequestId/{project_id}%2f{repository_id}%2f{pull_request_id}"


def _normalize_repository_key(repository_name: str | None) -> str:
    normalized_name = _normalize_lookup_key(repository_name)
    key = normalized_name.replace(" ", "_")
    if not key:
        raise ValueError("repository_name must contain letters or numbers.")
    return key


def _build_repository_aliases(repository_name: str | None) -> set[str]:
    normalized_repository_name = _normalize_lookup_key(repository_name)
    if not normalized_repository_name:
        return set()

    aliases = {
        normalized_repository_name,
        normalized_repository_name.replace(" ", "_"),
        normalized_repository_name.replace(" ", ""),
        normalized_repository_name.replace(" ", "-"),
    }
    return {alias for alias in aliases if alias}


def _resolve_git_repository(git: GitClient, repository_name: str) -> GitRepository:
    normalized_repository_name = str(repository_name or "").strip()
    if not normalized_repository_name:
        raise ValueError("repository_name e obrigatorio para o fluxo de branch/PR da historia.")

    requested_aliases = _build_repository_aliases(normalized_repository_name)
    requested_aliases.add(_normalize_lookup_key(normalized_repository_name))

    repositories = git.get_repositories(project=get_project())
    for repository in repositories or []:
        candidate_name = str(getattr(repository, "name", "")).strip()
        candidate_aliases = _build_repository_aliases(candidate_name)
        candidate_aliases.add(_normalize_lookup_key(candidate_name))
        if requested_aliases.intersection(candidate_aliases):
            return repository

    available_repositories = sorted(
        str(getattr(repository, "name", "")).strip()
        for repository in repositories or []
        if str(getattr(repository, "name", "")).strip()
    )
    raise ValueError(
        "Repositorio nao encontrado no Azure DevOps para o fluxo de branch/PR da historia. "
        f"Recebido: {normalized_repository_name}. Disponiveis: {', '.join(available_repositories)}"
    )


def _resolve_local_repository_path(repository_name: str | None) -> Path | None:
    projects_dir = WORKSPACE_ROOT / "projects"
    if not projects_dir.exists():
        return None

    requested_aliases = _build_repository_aliases(repository_name)
    requested_aliases.add(_normalize_lookup_key(repository_name))

    for candidate in projects_dir.iterdir():
        if not candidate.is_dir() or candidate.name == "documentacao":
            continue

        candidate_aliases = _build_repository_aliases(candidate.name)
        candidate_aliases.add(_normalize_lookup_key(candidate.name))
        if requested_aliases.intersection(candidate_aliases):
            return candidate

    return None


def _load_pull_request_template_context(repository_name: str) -> dict[str, Any]:
    repository_path = _resolve_local_repository_path(repository_name)
    if repository_path is None:
        return {
            "found": False,
            "path": None,
            "source": "default",
        }

    template_path = repository_path / "pull_request_template.md"
    if not template_path.exists():
        return {
            "found": False,
            "path": None,
            "source": "default",
        }

    return {
        "found": True,
        "path": str(template_path),
        "source": "repository",
    }


def _normalize_markdown_block(value: str | None, fallback: str = "Nao informado.") -> str:
    normalized_value = str(value or "").strip()
    return normalized_value or fallback


def _normalize_mermaid_block(value: str | None) -> str:
    normalized_value = str(value or "").strip()
    if not normalized_value:
        return ""

    if normalized_value.startswith("```"):
        if normalized_value.lower().startswith("```mermaid"):
            return normalized_value
        raise ValueError("mermaid_diagram deve ser conteudo Mermaid ou um bloco ```mermaid```.")

    return f"```mermaid\n{normalized_value}\n```"


def _build_checklist_entry(label: str, checked: bool, detail: str | None = None) -> str:
    entry = f"- [{'x' if checked else ' '}] {label}"
    normalized_detail = str(detail or "").strip()
    if normalized_detail:
        entry = f"{entry} {normalized_detail}"
    return entry


def _build_pull_request_body_markdown(
    what_was_changed: str,
    affected_processes: str,
    expected_impacts: str,
    important_points: str,
    mermaid_diagram: str,
    tests_updated: bool = False,
    tests_unchanged: bool = False,
    new_library_name: str | None = None,
    env_changes: str | None = None,
    generated_migration_or_seed: str | None = None,
    new_queue_or_command: str | None = None,
) -> str:
    normalized_mermaid = _normalize_mermaid_block(mermaid_diagram)
    lines = [
        "# O que foi modificado",
        _normalize_markdown_block(what_was_changed),
        "",
        "# Quais processos essa implementacao afeta",
        _normalize_markdown_block(affected_processes),
        "",
        "# Quais impactos esperados",
        _normalize_markdown_block(expected_impacts),
        "",
        "# Pontos importantes",
        _normalize_markdown_block(important_points),
        "",
        "# Checklist",
        "",
        "- Testes",
        f"  {_build_checklist_entry('Voce adicionou ou ajustou testes unitarios', tests_updated)}",
        f"  {_build_checklist_entry('Essa PR nao altera testes', tests_unchanged)}",
        "- Modificacoes",
        f"  {_build_checklist_entry('Voce adicionou alguma biblioteca nova? se sim qual:', bool(str(new_library_name or '').strip()), new_library_name)}",
        f"  {_build_checklist_entry('Voce alterou o .env? se sim qual:', bool(str(env_changes or '').strip()), env_changes)}",
        f"  {_build_checklist_entry('Voce gerou alguma nova migration/seed?', bool(str(generated_migration_or_seed or '').strip()), generated_migration_or_seed)}",
        f"  {_build_checklist_entry('Voce adicionou alguma nova fila/comando? se sim qual:', bool(str(new_queue_or_command or '').strip()), new_queue_or_command)}",
        "",
        "# Diagrama Mermaid",
        normalized_mermaid,
    ]
    return "\n".join(lines).strip()


def _extract_relation_name(relation: WorkItemRelation) -> str | None:
    attributes = getattr(relation, "attributes", None) or {}
    if isinstance(attributes, dict):
        relation_name = attributes.get("name")
    else:
        relation_name = getattr(attributes, "name", None)

    normalized_relation_name = str(relation_name or "").strip()
    return normalized_relation_name or None


def _extract_artifact_links(relations: list[Any] | None, allowed_names: set[str] | None = None) -> list[dict[str, Any]]:
    normalized_allowed_names = {name.lower() for name in (allowed_names or set())}
    artifact_links: list[dict[str, Any]] = []

    for relation in relations or []:
        if getattr(relation, "rel", "") != "ArtifactLink":
            continue

        relation_name = _extract_relation_name(relation)
        if normalized_allowed_names and str(relation_name or "").lower() not in normalized_allowed_names:
            continue

        artifact_links.append(
            {
                "name": relation_name,
                "url": getattr(relation, "url", None),
            }
        )

    return artifact_links


def _has_artifact_link(artifact_links: list[dict[str, Any]] | None, artifact_url: str) -> bool:
    normalized_artifact_url = str(artifact_url or "").strip().lower()
    if not normalized_artifact_url:
        return False

    return any(
        str(link.get("url") or "").strip().lower() == normalized_artifact_url
        for link in (artifact_links or [])
    )


def _build_artifact_link_patch(artifact_url: str, artifact_name: str) -> dict[str, Any]:
    return {
        "op": "add",
        "path": "/relations/-",
        "value": {
            "rel": "ArtifactLink",
            "url": artifact_url,
            "attributes": {
                "name": artifact_name,
            },
        },
    }


def _get_story_pr_work_item_fields() -> list[str]:
    return [
        "System.Id",
        "System.Title",
        "System.State",
        "System.WorkItemType",
        "System.IterationPath",
    ]


def _get_work_item_snapshot(wit: WorkItemTrackingClient, work_item_id: int) -> dict[str, Any]:
    item = wit.get_work_item(id=work_item_id, expand="Relations")
    fields = getattr(item, "fields", {}) or {}
    resolved_work_item_id = int(fields.get("System.Id") or work_item_id)
    return {
        "id": resolved_work_item_id,
        "revision": validate_revision(getattr(item, "rev", None)),
        "title": fields.get("System.Title"),
        "state": fields.get("System.State"),
        "work_item_type": fields.get("System.WorkItemType"),
        "iteration_path": fields.get("System.IterationPath"),
        "url": getattr(item, "url", None),
        "browser_url": _build_browser_work_item_url(resolved_work_item_id),
        "relations": getattr(item, "relations", None) or [],
    }


def _normalize_related_work_item_ids(related_work_item_ids: list[int] | None) -> list[int]:
    normalized_ids: list[int] = []
    seen_ids: set[int] = set()
    for raw_work_item_id in related_work_item_ids or []:
        normalized_work_item_id = int(raw_work_item_id)
        if normalized_work_item_id in seen_ids:
            continue
        seen_ids.add(normalized_work_item_id)
        normalized_ids.append(normalized_work_item_id)
    return normalized_ids


def _resolve_story_related_work_items(
    wit: WorkItemTrackingClient,
    story_id: int,
    related_work_item_ids: list[int] | None = None,
    include_child_work_items: bool = True,
) -> dict[str, Any]:
    normalized_story_id = int(story_id)
    parent_snapshot = _get_work_item_snapshot(wit, normalized_story_id)

    child_work_item_ids: list[int] = []
    if include_child_work_items:
        for relation in parent_snapshot["relations"]:
            if getattr(relation, "rel", "") != "System.LinkTypes.Hierarchy-Forward":
                continue

            child_work_item_id_raw = str(getattr(relation, "url", "")).rstrip("/").split("/")[-1]
            if not child_work_item_id_raw.isdigit():
                continue

            child_work_item_id = int(child_work_item_id_raw)
            if child_work_item_id not in child_work_item_ids:
                child_work_item_ids.append(child_work_item_id)

    extra_related_work_item_ids: list[int] = []
    ordered_work_item_ids = [normalized_story_id]
    seen_work_item_ids = {normalized_story_id}

    for child_work_item_id in child_work_item_ids:
        if child_work_item_id in seen_work_item_ids:
            continue
        seen_work_item_ids.add(child_work_item_id)
        ordered_work_item_ids.append(child_work_item_id)

    for related_work_item_id in _normalize_related_work_item_ids(related_work_item_ids):
        if related_work_item_id in seen_work_item_ids:
            continue
        seen_work_item_ids.add(related_work_item_id)
        ordered_work_item_ids.append(related_work_item_id)
        extra_related_work_item_ids.append(related_work_item_id)

    snapshots_by_id: dict[int, dict[str, Any]] = {normalized_story_id: parent_snapshot}
    for work_item_id in ordered_work_item_ids[1:]:
        snapshots_by_id[work_item_id] = _get_work_item_snapshot(wit, work_item_id)

    resolved_work_items: list[dict[str, Any]] = []
    for work_item_id in ordered_work_item_ids:
        snapshot = dict(snapshots_by_id[work_item_id])
        if work_item_id == normalized_story_id:
            relationship_to_story = "parent"
        elif work_item_id in child_work_item_ids:
            relationship_to_story = "child"
        else:
            relationship_to_story = "related"
        snapshot["relationship_to_story"] = relationship_to_story
        resolved_work_items.append(snapshot)

    return {
        "parent": dict(parent_snapshot),
        "child_work_item_ids": child_work_item_ids,
        "extra_related_work_item_ids": extra_related_work_item_ids,
        "work_items": resolved_work_items,
    }


def _build_story_branch_association_preview_payload(
    wit: WorkItemTrackingClient,
    repository: GitRepository,
    story_id: int,
    source_branch: str,
    related_work_item_ids: list[int] | None = None,
    include_child_work_items: bool = True,
) -> dict[str, Any]:
    repository_name = str(getattr(repository, "name", "")).strip()
    repository_id = str(getattr(repository, "id", "")).strip()
    if not repository_id:
        raise ValueError("Nao foi possivel resolver o repository id do Azure DevOps para o fluxo de branch da historia.")

    project_id = _resolve_repository_project_id(repository)
    normalized_branch_name, source_ref_name = _normalize_source_branch(source_branch)
    branch_artifact_uri = _build_branch_artifact_uri(project_id, repository_id, normalized_branch_name)
    work_item_context = _resolve_story_related_work_items(
        wit,
        story_id=story_id,
        related_work_item_ids=related_work_item_ids,
        include_child_work_items=include_child_work_items,
    )

    work_items_preview: list[dict[str, Any]] = []
    already_linked_count = 0
    pending_link_count = 0

    for snapshot in work_item_context["work_items"]:
        existing_artifact_links = _extract_artifact_links(
            snapshot.get("relations"),
            allowed_names={"Branch", "Pull Request"},
        )
        branch_linked = _has_artifact_link(existing_artifact_links, branch_artifact_uri)
        if branch_linked:
            already_linked_count += 1
        else:
            pending_link_count += 1

        work_items_preview.append(
            {
                "id": snapshot["id"],
                "revision": snapshot.get("revision"),
                "title": snapshot.get("title"),
                "state": snapshot.get("state"),
                "work_item_type": snapshot.get("work_item_type"),
                "iteration_path": snapshot.get("iteration_path"),
                "relationship_to_story": snapshot.get("relationship_to_story"),
                "browser_url": snapshot.get("browser_url"),
                "existing_artifact_links": existing_artifact_links,
                "branch_linked": branch_linked,
            }
        )

    parent_snapshot = work_item_context["parent"]
    return {
        "operation": "preview_story_branch_association",
        "story_id": int(story_id),
        "repository_name": repository_name,
        "repository_id": repository_id,
        "project": get_project(),
        "project_id": project_id,
        "source_branch": normalized_branch_name,
        "source_ref_name": source_ref_name,
        "branch_artifact_uri": branch_artifact_uri,
        "branch_browser_url": _build_branch_browser_url(repository_name, normalized_branch_name),
        "parent_work_item": {
            "id": parent_snapshot["id"],
            "revision": parent_snapshot.get("revision"),
            "title": parent_snapshot.get("title"),
            "state": parent_snapshot.get("state"),
            "work_item_type": parent_snapshot.get("work_item_type"),
            "browser_url": parent_snapshot.get("browser_url"),
        },
        "related_work_item_ids": [
            work_item["id"]
            for work_item in work_items_preview
            if work_item["relationship_to_story"] != "parent"
        ],
        "include_child_work_items": bool(include_child_work_items),
        "work_items": work_items_preview,
        "expected_revisions": {str(entry["id"]): entry["revision"] for entry in work_items_preview},
        "counts": {
            "total_work_items": len(work_items_preview),
            "already_linked": already_linked_count,
            "pending_link": pending_link_count,
        },
    }


def _check_association_revisions(wit: WorkItemTrackingClient, work_items: list[dict[str, Any]]) -> None:
    expected = EXPECTED_REVISIONS.get()
    target_ids = {int(entry["id"]) for entry in work_items}
    if set(expected) != target_ids:
        raise ValueError("expected_revisions must cover exactly every branch/PR target.")
    for work_item_id in sorted(target_ids):
        current = _get_work_item_snapshot(wit, work_item_id)
        if current["revision"] != expected[work_item_id]:
            raise RuntimeError(f"Azure revision conflict for work item {work_item_id}.")


def _associate_artifact_to_work_items(
    wit: WorkItemTrackingClient,
    work_items: list[dict[str, Any]],
    artifact_url: str,
    artifact_name: str,
) -> dict[str, Any]:
    associated_work_item_ids: list[int] = []
    already_linked_work_item_ids: list[int] = []
    results: list[dict[str, Any]] = []

    for work_item in work_items:
        current_snapshot = _get_work_item_snapshot(wit, int(work_item["id"]))
        if current_snapshot["revision"] != EXPECTED_REVISIONS.get().get(int(work_item["id"])):
            raise RuntimeError(f"Azure revision conflict for work item {work_item['id']}.")
        current_artifact_links = _extract_artifact_links(
            current_snapshot.get("relations"),
            allowed_names={"Branch", "Pull Request"},
        )
        work_item["existing_artifact_links"] = current_artifact_links
        already_linked = _has_artifact_link(current_artifact_links, artifact_url)

        if already_linked:
            already_linked_work_item_ids.append(int(work_item["id"]))
            action = "already_linked"
        else:
            updated_item = update_work_item(
                wit,
                document=[_build_artifact_link_patch(artifact_url, artifact_name)],
                id=int(work_item["id"]),
                observed_revision=current_snapshot.get("revision"),
            )
            work_item["revision"] = validate_revision(updated_item.rev)
            associated_work_item_ids.append(int(work_item["id"]))
            action = "associated"
            work_item["existing_artifact_links"] = current_artifact_links + [
                {
                    "name": artifact_name,
                    "url": artifact_url,
                }
            ]

        results.append(
            {
                "id": int(work_item["id"]),
                "title": work_item.get("title"),
                "relationship_to_story": work_item.get("relationship_to_story"),
                "action": action,
                "browser_url": work_item.get("browser_url"),
            }
        )

    return {
        "artifact_name": artifact_name,
        "artifact_url": artifact_url,
        "associated_work_item_ids": associated_work_item_ids,
        "already_linked_work_item_ids": already_linked_work_item_ids,
        "associated_count": len(associated_work_item_ids),
        "already_linked_count": len(already_linked_work_item_ids),
        "results": results,
    }


def _find_existing_active_pull_request(
    git: GitClient,
    repository_id: str,
    source_ref_name: str,
    target_ref_name: str,
) -> GitPullRequest | None:
    pull_requests = git.get_pull_requests(
        repository_id=repository_id,
        search_criteria=get_active_pr_search_criteria(),
        project=get_project(),
    )
    normalized_source_ref_name = str(source_ref_name or "").strip().lower()
    normalized_target_ref_name = str(target_ref_name or "").strip().lower()

    for pull_request in pull_requests or []:
        if str(getattr(pull_request, "source_ref_name", "")).strip().lower() != normalized_source_ref_name:
            continue
        if str(getattr(pull_request, "target_ref_name", "")).strip().lower() != normalized_target_ref_name:
            continue
        return pull_request

    return None


def _build_pull_request_summary(pull_request: GitPullRequest, repository_name: str) -> dict[str, Any]:
    pull_request_id = getattr(pull_request, "pull_request_id", None)
    return {
        "pull_request_id": pull_request_id,
        "title": getattr(pull_request, "title", None),
        "source_ref_name": getattr(pull_request, "source_ref_name", None),
        "target_ref_name": getattr(pull_request, "target_ref_name", None),
        "is_draft": bool(getattr(pull_request, "is_draft", False)),
        "artifact_id": getattr(pull_request, "artifact_id", None),
        "remote_url": getattr(pull_request, "remote_url", None),
        "web_url": _build_pull_request_browser_url(repository_name, pull_request_id),
    }


def _build_story_pull_request_preview_payload(
    wit: WorkItemTrackingClient,
    git: GitClient,
    repository: GitRepository,
    story_id: int,
    source_branch: str,
    what_was_changed: str,
    affected_processes: str,
    expected_impacts: str,
    important_points: str,
    mermaid_diagram: str,
    related_work_item_ids: list[int] | None = None,
    include_child_work_items: bool = True,
    target_branch: str | None = None,
    title: str | None = None,
    tests_updated: bool = False,
    tests_unchanged: bool = False,
    new_library_name: str | None = None,
    env_changes: str | None = None,
    generated_migration_or_seed: str | None = None,
    new_queue_or_command: str | None = None,
) -> dict[str, Any]:
    branch_preview = _build_story_branch_association_preview_payload(
        wit,
        repository=repository,
        story_id=story_id,
        source_branch=source_branch,
        related_work_item_ids=related_work_item_ids,
        include_child_work_items=include_child_work_items,
    )
    template_context = _load_pull_request_template_context(branch_preview["repository_name"])
    resolved_target_branch = _resolve_pull_request_target_branch(target_branch)
    target_ref_name = f"refs/heads/{resolved_target_branch}"
    parent_title = str(branch_preview["parent_work_item"].get("title") or f"KL-{story_id}").strip()
    resolved_title = str(title or "").strip() or f"KL-{story_id}: {parent_title}"
    body_markdown = _build_pull_request_body_markdown(
        what_was_changed=what_was_changed,
        affected_processes=affected_processes,
        expected_impacts=expected_impacts,
        important_points=important_points,
        mermaid_diagram=mermaid_diagram,
        tests_updated=tests_updated,
        tests_unchanged=tests_unchanged,
        new_library_name=new_library_name,
        env_changes=env_changes,
        generated_migration_or_seed=generated_migration_or_seed,
        new_queue_or_command=new_queue_or_command,
    )
    existing_active_pull_request = _find_existing_active_pull_request(
        git,
        repository_id=branch_preview["repository_id"],
        source_ref_name=branch_preview["source_ref_name"],
        target_ref_name=target_ref_name,
    )

    return {
        "operation": "preview_story_pull_request",
        "story_id": int(story_id),
        "repository_name": branch_preview["repository_name"],
        "repository_id": branch_preview["repository_id"],
        "project": branch_preview["project"],
        "project_id": branch_preview["project_id"],
        "source_branch": branch_preview["source_branch"],
        "source_ref_name": branch_preview["source_ref_name"],
        "target_branch": resolved_target_branch,
        "target_ref_name": target_ref_name,
        "is_draft": True,
        "title": resolved_title,
        "body_format": "markdown",
        "body_markdown": body_markdown,
        "template": template_context,
        "branch_association_preview": branch_preview,
        "expected_revisions": dict(branch_preview["expected_revisions"]),
        "existing_active_pull_request": (
            _build_pull_request_summary(existing_active_pull_request, branch_preview["repository_name"])
            if existing_active_pull_request is not None
            else None
        ),
        "can_open_pull_request": existing_active_pull_request is None,
    }


def _resolve_pull_request_target_branch(target_branch: str | None) -> str:
    _, ref_name = _normalize_source_branch(target_branch or DEFAULT_PULL_REQUEST_TARGET_BRANCH)
    return ref_name[len("refs/heads/"):]


def _get_technical_debt_context_path(repository_name: str) -> Path:
    context_file_name = f"{_normalize_repository_key(repository_name)}.json"
    return TECHNICAL_DEBT_CONTEXT_DIR / context_file_name


def _load_repo_technical_debt_context(repository_name: str) -> dict[str, Any]:
    context_path = _get_technical_debt_context_path(repository_name)
    if not context_path.exists():
        return {
            "repository_name": repository_name,
            "updated_at": None,
            "technical_debts": [],
        }

    try:
        return json.loads(context_path.read_text(encoding="utf-8"))
    except Exception:
        return {
            "repository_name": repository_name,
            "updated_at": None,
            "technical_debts": [],
        }


def _get_technical_debt_preview_cache_path(repository_name: str) -> Path:
    preview_file_name = f"{_normalize_repository_key(repository_name)}.json"
    return TECHNICAL_DEBT_PREVIEW_CACHE_DIR / preview_file_name


def _load_repo_technical_debt_preview_cache(repository_name: str) -> dict[str, Any]:
    cache_path = _get_technical_debt_preview_cache_path(repository_name)
    if not cache_path.exists():
        return {
            "repository_name": repository_name,
            "updated_at": None,
            "preview_entries": [],
        }

    try:
        return json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return {
            "repository_name": repository_name,
            "updated_at": None,
            "preview_entries": [],
        }


def _normalize_implementation_status(value: str | None) -> str:
    normalized_value = _normalize_lookup_key(value)
    status_aliases = {
        "": "nao_verificado",
        "nao verificado": "nao_verificado",
        "nao verificada": "nao_verificado",
        "not verified": "nao_verificado",
        "ja implementado": "ja_implementado",
        "ja implementada": "ja_implementado",
        "already implemented": "ja_implementado",
        "implemented": "ja_implementado",
        "parcialmente implementado": "parcialmente_implementado",
        "parcialmente implementada": "parcialmente_implementado",
        "partially implemented": "parcialmente_implementado",
        "partial": "parcialmente_implementado",
        "nao implementado": "nao_implementado",
        "nao implementada": "nao_implementado",
        "not implemented": "nao_implementado",
    }
    return status_aliases.get(normalized_value, normalized_value.replace(" ", "_") or "nao_verificado")


def _save_repo_technical_debt_preview_entry(
    repository_name: str,
    preview_entry: dict[str, Any],
) -> str:
    TECHNICAL_DEBT_PREVIEW_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_payload = _load_repo_technical_debt_preview_cache(repository_name)
    existing_entries = cache_payload.get("preview_entries", []) or []
    normalized_title = _normalize_lookup_key(preview_entry.get("title"))

    merged_entries = [
        entry for entry in existing_entries
        if _normalize_lookup_key(entry.get("title")) != normalized_title
    ]
    merged_entries.insert(0, preview_entry)

    final_payload = {
        "repository_name": repository_name,
        "updated_at": datetime.utcnow().isoformat() + "Z",
        "preview_entries": merged_entries,
    }
    cache_path = _get_technical_debt_preview_cache_path(repository_name)
    cache_path.write_text(_format_json(final_payload) + "\n", encoding="utf-8")
    return str(cache_path)


def _normalize_child_task_preview(task: object) -> dict[str, Any]:
    if not isinstance(task, dict):
        raise ValueError("Cada child task do preview deve ser um objeto JSON.")

    title = str(task.get("title") or "").strip()
    if not title:
        raise ValueError("Cada child task do preview precisa de title.")

    return {
        "title": title,
        "description": str(task.get("description") or "").strip() or None,
        "acceptance_criteria": str(task.get("acceptance_criteria") or "").strip() or None,
        "developer_suggestion": str(task.get("developer_suggestion") or "").strip() or None,
        "remaining_work_hours": task.get("remaining_work_hours"),
    }


def _save_technical_debt_preview_cache(
    repository_name: str,
    title: str,
    description: str | None = None,
    acceptance_criteria: str | None = None,
    target_iteration_path: str | None = None,
    story_points: float | None = None,
    implementation_status: str | None = None,
    implementation_notes: str | None = None,
    child_tasks: Any = None,
) -> str:
    normalized_repository_name = str(repository_name or "").strip()
    normalized_title = str(title or "").strip()
    if not normalized_repository_name:
        raise ValueError("repository_name e obrigatorio para salvar o preview provisório do debito tecnico.")
    if not normalized_title:
        raise ValueError("title e obrigatorio para salvar o preview provisório do debito tecnico.")

    normalized_child_tasks = [
        _normalize_child_task_preview(task)
        for task in (child_tasks or [])
    ]
    preview_entry = {
        "title": normalized_title,
        "description": str(description or "").strip() or None,
        "acceptance_criteria": str(acceptance_criteria or "").strip() or None,
        "target_iteration_path": str(target_iteration_path or "").strip() or None,
        "story_points": story_points,
        "implementation_status": _normalize_implementation_status(implementation_status),
        "implementation_notes": str(implementation_notes or "").strip() or None,
        "child_tasks": normalized_child_tasks,
        "updated_at": datetime.utcnow().isoformat() + "Z",
    }
    cache_path = _save_repo_technical_debt_preview_entry(normalized_repository_name, preview_entry)
    return _format_json(
        {
            "repository_name": normalized_repository_name,
            "title": normalized_title,
            "implementation_status": preview_entry["implementation_status"],
            "child_task_count": len(normalized_child_tasks),
            "cache_path": cache_path,
            "saved": True,
        }
    )


def _infer_repository_name(repository_name: str | None, title: str | None, description: str | None) -> str | None:
    normalized_repository_name = str(repository_name or "").strip()
    if normalized_repository_name:
        return normalized_repository_name

    normalized_title = str(title or "")
    title_match = re.search(r"\[\s*([A-Za-z0-9_\- ]+)\s*\]", normalized_title)
    if title_match:
        candidate = _normalize_repository_key(title_match.group(1))
        if candidate:
            return candidate

    search_space = _normalize_lookup_key(" ".join(part for part in [title or "", description or ""] if part))
    for repo_candidate in ("kl_store", "portal_backend", "portal_frontend", "admin_backend", "admin_frontend"):
        if _normalize_lookup_key(repo_candidate).replace(" ", "") in search_space.replace(" ", ""):
            return repo_candidate

    return None


def _is_technical_debt_type(work_item_type: str | None) -> bool:
    return _normalize_lookup_key(work_item_type) in {
        "technical debt",
        "technical debts",
        "tech debt",
        "tech debts",
        "debito tecnico",
        "debitos tecnicos",
    }


def _work_item_fields(item: WorkItem) -> dict[str, Any]:
    fields = item.fields
    if not isinstance(fields, dict):
        raise RuntimeError("Azure retornou um work item sem campos válidos.")
    return fields


def _get_work_items_in_batches(wit: WorkItemTrackingClient, ids: list[str], fields: list[str], batch_size: int = 75) -> list[WorkItem]:
    if not ids:
        return []

    items: list[WorkItem] = []
    for start_index in range(0, len(ids), batch_size):
        batch_ids = ids[start_index:start_index + batch_size]
        items.extend(wit.get_work_items(ids=batch_ids, fields=fields))
    return items


def _query_technical_debt_candidates() -> list[dict[str, Any]]:
    connection = get_client()
    wit = connection.clients.get_work_item_tracking_client()

    query = Wiql(query="""
        SELECT [System.Id], [System.Title], [System.State], [System.WorkItemType],
               [System.IterationPath], [System.AreaPath], [System.ChangedDate], [System.Tags]
        FROM WorkItems
        WHERE [System.WorkItemType] IN ('Technical Debt', 'Technical debt', 'Tech Debt')
          AND [System.State] <> 'Removed'
        ORDER BY [System.ChangedDate] DESC
    """)

    result = wit.query_by_wiql(query, team_context=get_team_context())
    if not result.work_items:
        return []

    ids = [str(work_item.id) for work_item in result.work_items]
    items = _get_work_items_in_batches(wit, ids, fields=[
        "System.Id",
        "System.Title",
        "System.State",
        "System.WorkItemType",
        "System.IterationPath",
        "System.AreaPath",
        "System.ChangedDate",
        "System.Tags",
        "System.Description",
    ])

    candidates: list[dict[str, Any]] = []
    for item in items:
        fields = item.fields or {}
        work_item_id = fields.get("System.Id", getattr(item, "id", None))
        candidates.append(
            {
                "id": work_item_id,
                "title": fields.get("System.Title"),
                "state": fields.get("System.State"),
                "work_item_type": fields.get("System.WorkItemType"),
                "iteration_path": fields.get("System.IterationPath"),
                "area_path": fields.get("System.AreaPath"),
                "changed_at": _serialize_datetime(fields.get("System.ChangedDate")),
                "tags": fields.get("System.Tags", ""),
                "description": _strip_html(fields.get("System.Description")),
                "url": _build_browser_work_item_url(work_item_id),
            }
        )
    return candidates


def _tokenize_similarity_text(value: str | None, repository_name: str | None = None) -> set[str]:
    normalized_value = _normalize_lookup_key(value)
    if not normalized_value:
        return set()

    repository_alias_tokens: set[str] = set()
    for alias in _build_repository_aliases(repository_name):
        repository_alias_tokens.update(alias.replace("_", " ").replace("-", " ").split())

    tokens = {
        token
        for token in normalized_value.split()
        if len(token) > 2 and token not in SIMILARITY_STOPWORDS and token not in repository_alias_tokens
    }
    return tokens


def _is_candidate_associated_with_repository(candidate: dict[str, Any], repository_name: str, known_ids: set[int]) -> bool:
    candidate_id = int(candidate.get("id") or 0)
    if candidate_id and candidate_id in known_ids:
        return True

    aliases = _build_repository_aliases(repository_name)
    if not aliases:
        return False

    search_space_parts = [
        str(candidate.get("title") or ""),
        str(candidate.get("description") or ""),
        str(candidate.get("tags") or ""),
        str(candidate.get("area_path") or ""),
    ]
    normalized_search_space = _normalize_lookup_key(" ".join(search_space_parts))
    collapsed_search_space = normalized_search_space.replace(" ", "")

    for alias in aliases:
        normalized_alias = _normalize_lookup_key(alias)
        collapsed_alias = normalized_alias.replace(" ", "")
        if normalized_alias and normalized_alias in normalized_search_space:
            return True
        if collapsed_alias and collapsed_alias in collapsed_search_space:
            return True

    return False


def _score_technical_debt_similarity(repository_name: str, title: str, description: str | None, candidate: dict[str, Any]) -> dict[str, Any]:
    normalized_title = _normalize_lookup_key(title)
    candidate_title = str(candidate.get("title") or "")
    normalized_candidate_title = _normalize_lookup_key(candidate_title)
    input_tokens = _tokenize_similarity_text(" ".join(part for part in [title, description or ""] if part), repository_name)
    candidate_tokens = _tokenize_similarity_text(
        " ".join(part for part in [candidate_title, str(candidate.get("description") or "")] if part),
        repository_name,
    )

    shared_tokens = sorted(input_tokens & candidate_tokens)
    union_tokens = input_tokens | candidate_tokens
    token_score = (len(shared_tokens) / len(union_tokens)) if union_tokens else 0.0
    title_ratio = SequenceMatcher(None, normalized_title, normalized_candidate_title).ratio() if normalized_title and normalized_candidate_title else 0.0
    score = max(title_ratio if shared_tokens else 0.0, (token_score * 0.7) + (title_ratio * 0.3))

    return {
        "exact_title_match": bool(normalized_title and normalized_title == normalized_candidate_title),
        "token_overlap": round(token_score, 3),
        "title_ratio": round(title_ratio, 3),
        "similarity_score": round(score, 3),
        "matched_terms": shared_tokens,
    }


def _build_technical_debt_similarity_analysis(
    repository_name: str,
    title: str,
    description: str | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    normalized_limit = max(1, min(int(limit or 5), 10))
    existing_context = _load_repo_technical_debt_context(repository_name)
    known_ids = {
        int(item.get("id"))
        for item in existing_context.get("technical_debts", [])
        if str(item.get("id") or "").isdigit()
    }

    candidates = _query_technical_debt_candidates()
    associated_items: list[dict[str, Any]] = []
    similar_items: list[dict[str, Any]] = []

    for candidate in candidates:
        if _is_candidate_associated_with_repository(candidate, repository_name, known_ids):
            associated_items.append(candidate)

        similarity = _score_technical_debt_similarity(repository_name, title, description, candidate)
        if similarity["exact_title_match"] or similarity["token_overlap"] >= 0.12 or similarity["similarity_score"] >= 0.55:
            similar_items.append({**candidate, **similarity})

    associated_items.sort(key=lambda item: ((item.get("state") or "") in COMPLETED_STATES, item.get("changed_at") or ""), reverse=True)
    similar_items.sort(
        key=lambda item: (
            item.get("exact_title_match", False),
            item.get("similarity_score", 0.0),
            item.get("changed_at") or "",
        ),
        reverse=True,
    )

    serialized_associated_items = [
        {
            "id": item.get("id"),
            "title": item.get("title"),
            "state": item.get("state"),
            "iteration_path": item.get("iteration_path"),
            "url": item.get("url"),
        }
        for item in associated_items
    ]
    return {
        "repository_name": repository_name,
        "query_title": title,
        "duplicate_found": any(item.get("exact_title_match") for item in similar_items),
        "similar_found": bool(similar_items),
        "suggested_matches": [
            {
                "id": item.get("id"),
                "title": item.get("title"),
                "state": item.get("state"),
                "iteration_path": item.get("iteration_path"),
                "similarity_score": item.get("similarity_score"),
                "token_overlap": item.get("token_overlap"),
                "title_ratio": item.get("title_ratio"),
                "exact_title_match": item.get("exact_title_match"),
                "matched_terms": item.get("matched_terms"),
                "url": item.get("url"),
            }
            for item in similar_items[:normalized_limit]
        ],
        "associated_repository_items": serialized_associated_items,
        "local_context_path": None,
        "context_persistence": "moved_to_langgraph",
    }


def _find_similar_technical_debts(
    repository_name: str,
    title: str,
    description: str | None = None,
    limit: int = 5,
) -> str:
    normalized_repository_name = str(repository_name or "").strip()
    normalized_title = str(title or "").strip()
    if not normalized_repository_name:
        raise ValueError("repository_name e obrigatorio para analisar debitos tecnicos parecidos.")
    if not normalized_title:
        raise ValueError("title e obrigatorio para analisar debitos tecnicos parecidos.")

    return _format_json(
        _build_technical_debt_similarity_analysis(
            repository_name=normalized_repository_name,
            title=normalized_title,
            description=description,
            limit=limit,
        )
    )


def _build_story_summary(description: str, acceptance_criteria: str, story: dict[str, Any]) -> str:
    summary_parts: list[str] = []

    if description and description != "Sem descricao":
        first_sentence = re.split(r"(?<=[.!?])\s+", description, maxsplit=1)[0].strip()
        if first_sentence:
            summary_parts.append(first_sentence)

    if acceptance_criteria and acceptance_criteria != "Sem criterios definidos":
        criteria_line = acceptance_criteria.splitlines()[0].strip()
        if criteria_line:
            if len(criteria_line) > 140:
                criteria_line = criteria_line[:137].rstrip() + "..."
            summary_parts.append(f"Criterio-chave: {criteria_line}")

    if not summary_parts:
        summary_parts.append(
            f"Historia {story.get('state', 'sem status')} com {story.get('story_points', 0)} story points."
        )

    summary = " ".join(summary_parts)
    if len(summary) > 280:
        summary = summary[:277].rstrip() + "..."
    return summary


def _serialize_datetime(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    raw_value = str(value).strip()
    return raw_value or None


def _extract_iteration_year(value: str | None) -> int | None:
    match = re.search(r"\\(20\d{2})(?:\\|$)", str(value or "").replace("/", "\\"))
    if not match:
        return None
    return int(match.group(1))


def _resolve_team_name() -> str | None:
    configured_team = os.environ.get("AZURE_TEAM", "").strip()
    if configured_team:
        return configured_team

    project = get_project()
    cached_team = TEAM_CONTEXT_CACHE.get(project)
    if cached_team:
        return cached_team

    connection = get_client()
    core_client = connection.clients.get_core_client()
    work_client = connection.clients.get_work_client()
    current_year = datetime.now().year
    team_candidates: list[dict[str, Any]] = []

    for team in core_client.get_teams(project_id=project):
        team_name = str(getattr(team, "name", "")).strip()
        if not team_name:
            continue

        team_context = TeamContext(project=project, team=team_name)
        try:
            current_iterations = work_client.get_team_iterations(team_context, timeframe="current")
        except Exception:
            continue

        for iteration in current_iterations or []:
            iteration_name = str(getattr(iteration, "name", "")).strip()
            iteration_path = str(getattr(iteration, "path", "")).strip()
            if not iteration_path:
                continue

            team_candidates.append(
                {
                    "team_name": team_name,
                    "iteration_name": iteration_name,
                    "iteration_path": iteration_path,
                    "iteration_year": _extract_iteration_year(iteration_path),
                    "sprint_number": _extract_sprint_number(iteration_name or iteration_path),
                }
            )

    if not team_candidates:
        return None

    selected_team = max(
        team_candidates,
        key=lambda candidate: (
            candidate["iteration_year"] == current_year,
            candidate["iteration_year"] or 0,
            candidate["sprint_number"] or -1,
            _is_plain_sprint_name(candidate["iteration_name"]),
            candidate["iteration_path"],
            candidate["team_name"],
        ),
    )
    TEAM_CONTEXT_CACHE[project] = selected_team["team_name"]
    return selected_team["team_name"]


def _get_current_sprint_context() -> dict[str, Any]:
    connection = get_client()
    work_client = connection.clients.get_work_client()
    current_iterations = work_client.get_team_iterations(get_team_context(), timeframe="current")

    if not current_iterations:
        return {
            "name": None,
            "path": None,
            "start_date": None,
            "finish_date": None,
            "timeframe": None,
            "goal": None,
            "goal_available": False,
            "goal_note": "Nenhum sprint atual retornado pela API de iteracoes do Azure DevOps.",
        }

    current_iteration = current_iterations[0]
    attributes = getattr(current_iteration, "attributes", None)
    context = {
        "name": getattr(current_iteration, "name", None),
        "path": getattr(current_iteration, "path", None),
        "start_date": _serialize_datetime(getattr(attributes, "start_date", None)),
        "finish_date": _serialize_datetime(getattr(attributes, "finish_date", None)),
        "timeframe": getattr(attributes, "time_frame", None),
        "goal": None,
        "goal_available": False,
        "goal_note": "A API de iteracoes do Azure DevOps usada por esta integracao nao expoe o sprint goal no payload atual.",
    }

    try:
        org_url = os.environ["AZURE_ORG_URL"].rstrip("/")
        project = quote(get_project(), safe="")
        pat = os.environ["AZURE_PAT"]
        detail_url = f"{org_url}/{project}/_apis/work/teamsettings/iterations?$timeframe=current&api-version=7.1-preview.1"
        auth = b64encode(f":{pat}".encode()).decode()
        request = Request(detail_url, headers={"Authorization": f"Basic {auth}"})
        with urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode())

        values = payload.get("value") or []
        if values:
            goal = values[0].get("goal")
            if isinstance(goal, str) and goal.strip():
                context["goal"] = _normalize_whitespace(goal)
                context["goal_available"] = True
                context["goal_note"] = None
    except Exception as error:
        logging.getLogger(__name__).debug("Consulta da meta da sprint indisponível (%s).", type(error).__name__)

    return context


def _serialize_iteration(iteration: TeamSettingsIteration) -> dict[str, Any]:
    attributes = getattr(iteration, "attributes", None)
    return {
        "name": getattr(iteration, "name", None),
        "path": getattr(iteration, "path", None),
        "start_date": _serialize_datetime(getattr(attributes, "start_date", None)),
        "finish_date": _serialize_datetime(getattr(attributes, "finish_date", None)),
        "timeframe": getattr(attributes, "time_frame", None),
    }


def _get_all_iterations_context() -> list[dict[str, Any]]:
    connection = get_client()
    work_client = connection.clients.get_work_client()
    iterations = work_client.get_team_iterations(get_team_context())
    serialized_iterations = [_serialize_iteration(iteration) for iteration in iterations]
    serialized_iterations.sort(
        key=lambda iteration: (
            iteration["start_date"] is None,
            iteration["start_date"] or "",
            iteration["path"] or "",
        )
    )
    return serialized_iterations


def _get_future_iterations_context(include_undated: bool = False) -> list[dict[str, Any]]:
    future_iterations = [
        iteration
        for iteration in _get_all_iterations_context()
        if str(iteration.get("timeframe") or "").strip().lower() == "future"
    ]

    if include_undated:
        return future_iterations

    return [
        iteration
        for iteration in future_iterations
        if iteration.get("start_date") or iteration.get("finish_date")
    ]


def _normalize_classification_iteration_path(path: str | None) -> str | None:
    normalized_path = str(path or "").strip().replace("/", "\\")
    if not normalized_path:
        return None

    if normalized_path.startswith("\\"):
        normalized_path = normalized_path[1:]

    iteration_segment = "\\Iteration\\"
    if iteration_segment in normalized_path:
        normalized_path = normalized_path.replace(iteration_segment, "\\", 1)
    elif normalized_path.endswith("\\Iteration"):
        normalized_path = normalized_path[: -len("\\Iteration")]

    return normalized_path or None


def _extract_sprint_number(value: str | None) -> int | None:
    match = re.search(r"\bsprint\s*0*(\d+)\b", str(value or ""), flags=re.IGNORECASE)
    if not match:
        return None
    return int(match.group(1))


def _is_plain_sprint_name(value: str | None) -> bool:
    return bool(re.fullmatch(r"sprint\s*0*\d+", str(value or "").strip(), flags=re.IGNORECASE))


def _get_current_year_sprint_contexts() -> list[dict[str, Any]]:
    current_year = datetime.now().year
    connection = get_client()
    wit = connection.clients.get_work_item_tracking_client()
    root = wit.get_classification_node(project=get_project(), structure_group="iterations", depth=4)

    current_year_node = next(
        (
            child
            for child in getattr(root, "children", None) or []
            if str(getattr(child, "name", "")).strip() == str(current_year)
        ),
        None,
    )

    if current_year_node is None:
        available_years = ", ".join(
            str(getattr(child, "name", "")).strip() or "Sem nome"
            for child in getattr(root, "children", None) or []
        )
        raise ValueError(
            "Nenhum ramo de iteracao do ano atual foi encontrado no Azure DevOps. "
            f"Ano esperado: {current_year}. Ramos disponiveis: {available_years}"
        )

    sprint_candidates: list[dict[str, Any]] = []
    for child in getattr(current_year_node, "children", None) or []:
        sprint_number = _extract_sprint_number(getattr(child, "name", None))
        normalized_path = _normalize_classification_iteration_path(getattr(child, "path", None))
        if sprint_number is None or not normalized_path:
            continue

        attributes = getattr(child, "attributes", None)
        sprint_candidates.append(
            {
                "name": getattr(child, "name", None),
                "path": normalized_path,
                "start_date": _serialize_datetime(getattr(attributes, "start_date", None)),
                "finish_date": _serialize_datetime(getattr(attributes, "finish_date", None)),
                "timeframe": "future",
                "year": current_year,
                "sprint_number": sprint_number,
                "resolution_strategy": "current_year_highest_sprint",
            }
        )

    if not sprint_candidates:
        raise ValueError(
            "Nenhuma sprint numerada foi encontrada no ramo de iteracoes do ano atual no Azure DevOps. "
            f"Ano esperado: {current_year}"
        )

    sprint_candidates.sort(
        key=lambda candidate: (
            candidate["sprint_number"],
            _is_plain_sprint_name(candidate.get("name")),
            candidate["path"],
        )
    )
    return sprint_candidates


def _get_current_year_highest_sprint_context() -> dict[str, Any]:
    return _get_current_year_sprint_contexts()[-1]


def _get_next_sprint_from_current_context() -> dict[str, Any]:
    current_sprint_context = _get_current_sprint_context()
    current_sprint_number = _extract_sprint_number(current_sprint_context.get("name") or current_sprint_context.get("path"))
    if current_sprint_number is None:
        raise ValueError(
            "Nao foi possivel identificar o numero da sprint atual para resolver a proxima sprint. "
            f"Sprint atual retornada: {current_sprint_context.get('path') or current_sprint_context.get('name') or 'sem identificador'}"
        )

    next_sprint_number = current_sprint_number + 1
    sprint_candidates = _get_current_year_sprint_contexts()
    matching_candidates = [
        candidate for candidate in sprint_candidates if candidate["sprint_number"] == next_sprint_number
    ]
    next_sprint = None
    if matching_candidates:
        next_sprint = max(
            matching_candidates,
            key=lambda candidate: (
                _is_plain_sprint_name(candidate.get("name")),
                candidate.get("path") or "",
            ),
        )
    if next_sprint is None:
        available_sprints = ", ".join(
            str(candidate.get("name") or candidate.get("path") or "Sem identificador")
            for candidate in sprint_candidates
        )
        raise ValueError(
            "Nao foi encontrada uma sprint correspondente a sprint atual + 1 no ramo do ano atual. "
            f"Sprint atual: {current_sprint_context.get('path') or current_sprint_context.get('name')}. "
            f"Esperada: Sprint {next_sprint_number}. Disponiveis: {available_sprints}"
        )

    next_sprint["resolution_strategy"] = "current_sprint_plus_one"
    next_sprint["based_on_current_sprint"] = current_sprint_context.get("path") or current_sprint_context.get("name")
    return next_sprint


def _get_next_sprint_context() -> dict[str, Any]:
    next_sprint = _get_next_sprint_from_current_context()
    if not str(next_sprint.get("path") or "").strip():
        raise ValueError("A proxima sprint encontrada nao possui iteration path utilizavel.")

    return next_sprint


def _append_work_item_field_patch(document: list[dict[str, Any]], field_name: str, value: str | float | None) -> None:
    if value is None:
        return

    normalized_value = value.strip() if isinstance(value, str) else value
    if normalized_value == "":
        return

    document.append({"op": "add", "path": f"/fields/{field_name}", "value": normalized_value})


def _normalize_lookup_key(value: str | None) -> str:
    normalized_value = unicodedata.normalize("NFKD", str(value or ""))
    ascii_value = normalized_value.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", " ", ascii_value.lower()).strip()


def _get_available_work_item_types(wit: WorkItemTrackingClient) -> dict[str, str]:
    project = get_project()
    cached_types = WORK_ITEM_TYPE_CACHE.get(project)
    if cached_types is not None:
        return cached_types

    available_types: dict[str, str] = {}
    for work_item_type in wit.get_work_item_types(project):
        type_name = str(getattr(work_item_type, "name", "")).strip()
        if not type_name:
            continue
        available_types[_normalize_lookup_key(type_name)] = type_name

    WORK_ITEM_TYPE_CACHE[project] = available_types
    return available_types


def _resolve_work_item_type_name(wit: WorkItemTrackingClient, requested_work_item_type: str) -> str:
    normalized_requested_type = _normalize_lookup_key(requested_work_item_type)
    if not normalized_requested_type:
        raise ValueError("work_item_type e obrigatorio para criar um work item.")

    available_types = _get_available_work_item_types(wit)

    direct_match = available_types.get(normalized_requested_type)
    if direct_match:
        return direct_match

    for alias_candidate in WORK_ITEM_TYPE_ALIASES.get(normalized_requested_type, ()):
        resolved_candidate = available_types.get(_normalize_lookup_key(alias_candidate))
        if resolved_candidate:
            return resolved_candidate

    supported_type_names = sorted(set(available_types.values()))
    raise ValueError(
        "Tipo de work item nao encontrado neste projeto. "
        f"Recebido: {requested_work_item_type}. Disponiveis: {', '.join(supported_type_names)}"
    )


def _extract_reference_name(field_definition: WorkItemTypeFieldWithReferences | WorkItemField) -> str | None:
    reference_name = getattr(field_definition, "reference_name", None)
    if isinstance(reference_name, str) and reference_name.strip():
        return reference_name.strip()

    field_reference = getattr(field_definition, "field", None)
    reference_name = getattr(field_reference, "reference_name", None)
    if isinstance(reference_name, str) and reference_name.strip():
        return reference_name.strip()

    return None


def _get_supported_work_item_fields(wit: WorkItemTrackingClient, work_item_type: str) -> set[str]:
    project = get_project()
    cache_key = (project, work_item_type)
    cached_fields = WORK_ITEM_FIELD_CACHE.get(cache_key)
    if cached_fields is not None:
        return cached_fields

    supported_fields: set[str] = set()
    try:
        field_definitions = wit.get_work_item_type_fields_with_references(project, work_item_type)
    except Exception:
        field_definitions = []

    for field_definition in field_definitions or []:
        reference_name = _extract_reference_name(field_definition)
        if reference_name:
            supported_fields.add(reference_name)

    if not supported_fields:
        supported_fields = set(WORK_ITEM_FIELD_ALIASES)

    WORK_ITEM_FIELD_CACHE[cache_key] = supported_fields
    return supported_fields


def _append_supported_work_item_field_patch(
    document: list[dict[str, Any]],
    supported_fields: set[str],
    field_name: str,
    value: str | float | None,
    applied_fields: list[str],
    skipped_fields: list[str],
) -> None:
    if value is None:
        return

    normalized_value = value.strip() if isinstance(value, str) else value
    if normalized_value == "":
        return

    logical_field_name = WORK_ITEM_FIELD_ALIASES.get(field_name, field_name)
    if field_name not in supported_fields:
        skipped_fields.append(logical_field_name)
        return

    _append_work_item_field_patch(document, field_name, normalized_value)
    applied_fields.append(logical_field_name)


def _build_work_item_relation_url(work_item_id: int) -> str:
    return f"{os.environ['AZURE_ORG_URL'].rstrip('/')}/_apis/wit/workItems/{work_item_id}"


def _append_parent_relation_patch(document: list[dict[str, Any]], parent_work_item_id: int | None) -> None:
    if parent_work_item_id is None:
        return

    document.append(
        {
            "op": "add",
            "path": "/relations/-",
            "value": {
                "rel": "System.LinkTypes.Hierarchy-Reverse",
                "url": _build_work_item_relation_url(parent_work_item_id),
            },
        }
    )


def _build_effort_payload(
    story_points: float | None = None,
    remaining_work_hours: float | None = None,
    completed_work_hours: float | None = None,
    original_estimate_hours: float | None = None,
) -> dict[str, float]:
    payload: dict[str, float] = {}
    if story_points is not None:
        payload["story_points"] = story_points
    if remaining_work_hours is not None:
        payload["remaining_work_hours"] = remaining_work_hours
    if completed_work_hours is not None:
        payload["completed_work_hours"] = completed_work_hours
    if original_estimate_hours is not None:
        payload["original_estimate_hours"] = original_estimate_hours
    return payload


def _serialize_work_item_payload(item: WorkItem, extra_payload: Optional[dict[str, Any]] = None) -> str:
    fields = getattr(item, "fields", {}) or {}
    payload = {
        "id": fields.get("System.Id", getattr(item, "id", None)),
        "revision": validate_revision(getattr(item, "rev", None)),
        "title": fields.get("System.Title"),
        "work_item_type": fields.get("System.WorkItemType"),
        "state": fields.get("System.State"),
        "current_status": fields.get("System.State"),
        "assigned_to": _serialize_assigned_to(fields.get("System.AssignedTo")),
        "area_path": fields.get("System.AreaPath"),
        "iteration_path": fields.get("System.IterationPath"),
        "story_points": fields.get("Microsoft.VSTS.Scheduling.StoryPoints"),
        "remaining_work_hours": fields.get("Microsoft.VSTS.Scheduling.RemainingWork"),
        "completed_work_hours": fields.get("Microsoft.VSTS.Scheduling.CompletedWork"),
        "original_estimate_hours": fields.get("Microsoft.VSTS.Scheduling.OriginalEstimate"),
        "url": getattr(item, "url", None),
    }
    if extra_payload:
        payload.update(extra_payload)
    return _format_json(payload)


def _confirm_work_item_status(item: WorkItem, work_item_id: int, status: str | None) -> None:
    if getattr(item, "id", None) != work_item_id:
        raise RuntimeError("Azure returned a different work item after the update.")
    validate_revision(getattr(item, "rev", None))
    if status is not None and (getattr(item, "fields", {}) or {}).get("System.State") != status:
        raise RuntimeError("Azure returned a status mismatch after the update.")


def _is_blocked_item(fields: dict[str, Any]) -> bool:
    state = str(fields.get("System.State", "")).strip().lower()
    board_column = str(fields.get("System.BoardColumn", "")).strip().lower()
    tags = str(fields.get("System.Tags", "")).strip().lower()
    blocked_tokens = ("blocked", "bloqueado", "impeded", "impediment")
    return (
        any(token in state for token in blocked_tokens)
        or any(token in board_column for token in blocked_tokens)
        or any(token in tags for token in blocked_tokens)
    )


def _is_completed_state(state: str | None) -> bool:
    return str(state or "").strip().lower() in COMPLETED_STATES


def _serialize_assigned_to(value: IdentityRef | dict[str, object] | str | None) -> str:
    if isinstance(value, dict):
        return str(value.get("displayName") or value.get("uniqueName") or "Nao atribuido")

    display_name = getattr(value, "display_name", None) or getattr(value, "displayName", None)
    unique_name = getattr(value, "unique_name", None) or getattr(value, "uniqueName", None)
    if display_name:
        return str(display_name)
    if unique_name:
        return str(unique_name)

    raw_value = str(value or "").strip()
    return raw_value or "Nao atribuido"


def _query_assigned_items(email: str, sprint_path: str | None = None) -> list[dict[str, Any]]:
    connection = get_client()
    wit = connection.clients.get_work_item_tracking_client()
    escaped_email = email.replace("'", "''")
    iteration = "'{}'".format(sprint_path.replace("'", "''")) if sprint_path else "@CurrentIteration"

    query = Wiql(query=f"""
        SELECT [System.Id], [System.Title], [System.State], [System.WorkItemType],
               [System.IterationPath], [System.ChangedDate],
               [Microsoft.VSTS.Scheduling.StoryPoints], [Microsoft.VSTS.Scheduling.RemainingWork],
               [System.Tags]
        FROM WorkItems
        WHERE [System.AssignedTo] = '{escaped_email}'
          AND [System.IterationPath] = {iteration}
                    AND (
                                [System.WorkItemType] = 'Task'
                                OR [System.State] NOT IN {ACTIVE_DAILY_SUMMARY_EXCLUDED_STATES}
                    )
        ORDER BY [System.ChangedDate] DESC
    """)

    result = wit.query_by_wiql(query, team_context=get_team_context())
    if not result.work_items:
        return []

    ids = [str(work_item.id) for work_item in result.work_items]
    items = _get_work_items_in_batches(wit, ids=ids, fields=[
        "System.Id", "System.Title", "System.State", "System.WorkItemType",
        "System.IterationPath", "System.ChangedDate", "System.Tags",
        "Microsoft.VSTS.Scheduling.StoryPoints", "Microsoft.VSTS.Scheduling.RemainingWork"
    ])

    serialized_items: list[dict[str, Any]] = []
    for item in items:
        fields = _work_item_fields(item)
        serialized_items.append({
            "id": fields.get("System.Id"),
            "title": fields.get("System.Title"),
            "state": fields.get("System.State"),
            "work_item_type": fields.get("System.WorkItemType"),
            "iteration_path": fields.get("System.IterationPath"),
            "changed_at": str(fields.get("System.ChangedDate", "")),
            "story_points": fields.get("Microsoft.VSTS.Scheduling.StoryPoints") or 0,
            "remaining_work_hours": fields.get("Microsoft.VSTS.Scheduling.RemainingWork") or 0,
            "tags": fields.get("System.Tags", ""),
            "is_blocked": _is_blocked_item(fields),
        })
    return serialized_items


def _count_items_by_type(items: list[dict[str, Any]]) -> dict[str, int]:
    type_breakdown: dict[str, int] = {}
    for item in items:
        item_type = str(item.get("work_item_type") or "Desconhecido").strip() or "Desconhecido"
        type_breakdown[item_type] = type_breakdown.get(item_type, 0) + 1
    return dict(sorted(type_breakdown.items(), key=lambda entry: entry[0].lower()))


def _is_story_type(work_item_type: str | None) -> bool:
    return _normalize_lookup_key(work_item_type) in {"user story", "story"}


def _is_task_type(work_item_type: str | None) -> bool:
    return _normalize_lookup_key(work_item_type) == "task"


def _count_items_by_state(items: list[dict[str, Any]]) -> dict[str, int]:
    state_breakdown: dict[str, int] = {}
    for item in items:
        state = str(item.get("state") or "Desconhecido").strip() or "Desconhecido"
        state_breakdown[state] = state_breakdown.get(state, 0) + 1
    return dict(sorted(state_breakdown.items(), key=lambda entry: entry[0].lower()))


def _query_child_items_summary(parent_items: list[dict[str, Any]]) -> dict[str, Any]:
    if not parent_items:
        return {
            "total_children": 0,
            "open_children": 0,
            "blocked_children": 0,
            "completed_children": 0,
            "children_by_type": {},
            "parents_with_children": 0,
            "children_by_parent": [],
        }

    connection = get_client()
    wit = connection.clients.get_work_item_tracking_client()

    parent_lookup = {
        int(item["id"]): item
        for item in parent_items
        if item.get("id") is not None
    }
    child_ids_by_parent: dict[int, list[int]] = {}
    unique_child_ids: list[int] = []
    seen_child_ids: set[int] = set()

    for parent_id in parent_lookup:
        parent_work_item = wit.get_work_item(id=parent_id, expand="Relations")
        relations = getattr(parent_work_item, "relations", None) or []
        child_ids: list[int] = []
        for relation in relations:
            if getattr(relation, "rel", "") != "System.LinkTypes.Hierarchy-Forward":
                continue

            child_id_raw = str(getattr(relation, "url", "")).rstrip("/").split("/")[-1]
            if not child_id_raw.isdigit():
                continue

            child_id = int(child_id_raw)
            child_ids.append(child_id)
            if child_id not in seen_child_ids:
                seen_child_ids.add(child_id)
                unique_child_ids.append(child_id)

        if child_ids:
            child_ids_by_parent[parent_id] = child_ids

    if not unique_child_ids:
        return {
            "total_children": 0,
            "open_children": 0,
            "blocked_children": 0,
            "completed_children": 0,
            "children_by_type": {},
            "parents_with_children": 0,
            "children_by_parent": [],
        }

    child_items = _get_work_items_in_batches(wit, ids=[str(child_id) for child_id in unique_child_ids], fields=[
        "System.Id", "System.Title", "System.State", "System.WorkItemType",
        "System.IterationPath", "System.ChangedDate", "System.Tags",
        "Microsoft.VSTS.Scheduling.StoryPoints", "Microsoft.VSTS.Scheduling.RemainingWork"
    ])

    serialized_children: dict[int, dict[str, Any]] = {}
    for item in child_items:
        fields = _work_item_fields(item)
        state = fields.get("System.State")
        child_id = int(fields["System.Id"])
        serialized_children[child_id] = {
            "id": child_id,
            "title": fields.get("System.Title"),
            "state": state,
            "work_item_type": fields.get("System.WorkItemType"),
            "iteration_path": fields.get("System.IterationPath"),
            "changed_at": str(fields.get("System.ChangedDate", "")),
            "story_points": fields.get("Microsoft.VSTS.Scheduling.StoryPoints") or 0,
            "remaining_work_hours": fields.get("Microsoft.VSTS.Scheduling.RemainingWork") or 0,
            "tags": fields.get("System.Tags", ""),
            "is_blocked": _is_blocked_item(fields),
            "is_completed": _is_completed_state(state),
        }

    serialized_child_list = list(serialized_children.values())
    children_by_parent: list[dict[str, Any]] = []
    for parent_id, child_ids in child_ids_by_parent.items():
        child_entries = [serialized_children[child_id] for child_id in child_ids if child_id in serialized_children]
        if not child_entries:
            continue

        children_by_parent.append({
            "parent_id": parent_id,
            "parent_title": parent_lookup[parent_id].get("title"),
            "child_count": len(child_entries),
            "open_child_count": sum(1 for child in child_entries if not child.get("is_completed")),
            "blocked_child_count": sum(1 for child in child_entries if child.get("is_blocked") and not child.get("is_completed")),
            "children": child_entries,
        })

    children_by_parent.sort(key=lambda entry: (-entry["blocked_child_count"], -entry["open_child_count"], entry["parent_id"]))

    return {
        "total_children": len(serialized_child_list),
        "open_children": sum(1 for child in serialized_child_list if not child.get("is_completed")),
        "blocked_children": sum(1 for child in serialized_child_list if child.get("is_blocked") and not child.get("is_completed")),
        "completed_children": sum(1 for child in serialized_child_list if child.get("is_completed")),
        "children_by_type": _count_items_by_type(serialized_child_list),
        "parents_with_children": len(children_by_parent),
        "children_by_parent": children_by_parent,
    }


def _query_sprint_stories(include_details: bool = False, sprint_path: str | None = None) -> list[dict[str, Any]]:
    connection = get_client()
    wit = connection.clients.get_work_item_tracking_client()

    iteration = "'{}'".format(sprint_path.replace("'", "''")) if sprint_path else "@CurrentIteration"
    query = Wiql(query=f"""
        SELECT [System.Id], [System.Title], [System.State], [System.AssignedTo],
               [System.IterationPath], [System.ChangedDate],
               [Microsoft.VSTS.Scheduling.StoryPoints], [System.Tags]
        FROM WorkItems
        WHERE [System.WorkItemType] = 'User Story'
          AND [System.IterationPath] = {iteration}
          AND [System.State] <> 'Removed'
        ORDER BY [Microsoft.VSTS.Common.Priority] ASC, [System.ChangedDate] DESC
    """)

    result = wit.query_by_wiql(query, team_context=get_team_context())
    if not result.work_items:
        return []

    ids = [str(work_item.id) for work_item in result.work_items]
    fields = [
        "System.Id", "System.Title", "System.State", "System.AssignedTo",
        "System.IterationPath", "System.ChangedDate", "System.Tags",
        "Microsoft.VSTS.Scheduling.StoryPoints"
    ]
    if include_details:
        fields.extend([
            "System.Description",
            "Microsoft.VSTS.Common.AcceptanceCriteria",
        ])

    items = _get_work_items_in_batches(wit, ids=ids, fields=fields)

    serialized_stories: list[dict[str, Any]] = []
    for item in items:
        fields = _work_item_fields(item)
        state = fields.get("System.State")
        serialized_stories.append({
            "id": fields.get("System.Id"),
            "title": fields.get("System.Title"),
            "state": state,
            "assigned_to": _serialize_assigned_to(fields.get("System.AssignedTo")),
            "iteration_path": fields.get("System.IterationPath"),
            "changed_at": str(fields.get("System.ChangedDate", "")),
            "story_points": fields.get("Microsoft.VSTS.Scheduling.StoryPoints") or 0,
            "tags": fields.get("System.Tags", ""),
            "is_blocked": _is_blocked_item(fields),
            "is_completed": _is_completed_state(state),
        })
        if include_details:
            description = _strip_html(fields.get("System.Description")) or "Sem descricao"
            acceptance_criteria = _strip_html(
                fields.get("Microsoft.VSTS.Common.AcceptanceCriteria")
            ) or "Sem criterios definidos"
            serialized_stories[-1]["description"] = description
            serialized_stories[-1]["acceptance_criteria"] = acceptance_criteria
            serialized_stories[-1]["summary"] = _build_story_summary(
                description,
                acceptance_criteria,
                serialized_stories[-1],
            )
    return serialized_stories


def _build_sprint_stories_payload(
    sprint_stories: list[dict[str, Any]],
    detail_mode: str,
    sprint_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "project": get_project(),
        "detail_mode": detail_mode,
        "sprint_context": sprint_context or _get_current_sprint_context(),
        "counts": {
            "sprint_stories": len(sprint_stories),
        },
        "project_progress": _build_project_progress(sprint_stories),
        "sprint_stories": sprint_stories,
    }


def _build_project_progress(sprint_stories: list[dict[str, Any]]) -> dict[str, Any]:
    state_breakdown: dict[str, int] = {}
    completed_stories = 0
    blocked_stories = 0
    unassigned_stories = 0
    unestimated_stories = 0
    active_unestimated_stories = 0
    total_story_points = 0.0
    completed_story_points = 0.0
    blocked_story_points = 0.0

    for story in sprint_stories:
        state = str(story.get("state") or "Desconhecido").strip() or "Desconhecido"
        state_breakdown[state] = state_breakdown.get(state, 0) + 1

        story_points = float(story.get("story_points") or 0)
        total_story_points += story_points

        if story_points <= 0:
            unestimated_stories += 1
            if not story.get("is_completed"):
                active_unestimated_stories += 1

        if story.get("is_completed"):
            completed_stories += 1
            completed_story_points += story_points

        if story.get("is_blocked") and not story.get("is_completed"):
            blocked_stories += 1
            blocked_story_points += story_points

        if story.get("assigned_to") in {"", "Nao atribuido"}:
            unassigned_stories += 1

    total_stories = len(sprint_stories)
    active_stories = total_stories - completed_stories
    remaining_story_points = max(total_story_points - completed_story_points, 0.0)
    progress_percent_by_story_count = round((completed_stories / total_stories) * 100, 2) if total_stories else 0.0
    progress_percent_by_story_points = round((completed_story_points / total_story_points) * 100, 2) if total_story_points else 0.0
    progress_warning = None
    if active_unestimated_stories:
        progress_warning = (
            f"Existem {active_unestimated_stories} historias ativas sem story points; "
            "o progresso por story points pode superestimar o andamento do sprint."
        )

    return {
        "total_stories": total_stories,
        "completed_stories": completed_stories,
        "active_stories": active_stories,
        "blocked_stories": blocked_stories,
        "unassigned_stories": unassigned_stories,
        "unestimated_stories": unestimated_stories,
        "active_unestimated_stories": active_unestimated_stories,
        "total_story_points": round(total_story_points, 2),
        "completed_story_points": round(completed_story_points, 2),
        "blocked_story_points": round(blocked_story_points, 2),
        "remaining_story_points": round(remaining_story_points, 2),
        "progress_percent_by_story_count": progress_percent_by_story_count,
        "progress_percent_by_story_points": progress_percent_by_story_points,
        "progress_warning": progress_warning,
        "state_breakdown": state_breakdown,
    }


def _available_sprint_contexts() -> list[dict[str, Any]]:
    wit = get_client().clients.get_work_item_tracking_client()
    root = wit.get_classification_node(project=get_project(), structure_group="iterations", depth=6)
    contexts: list[dict[str, Any]] = []

    def collect(node: WorkItemClassificationNode) -> None:
        name = str(getattr(node, "name", "") or "")
        sprint_path = _normalize_classification_iteration_path(getattr(node, "path", None))
        if sprint_path and _extract_sprint_number(name) is not None:
            attributes = getattr(node, "attributes", None)
            if isinstance(attributes, dict):
                start = attributes.get("startDate")
                finish = attributes.get("finishDate")
            else:
                start = getattr(attributes, "start_date", None)
                finish = getattr(attributes, "finish_date", None)
            contexts.append({"name": name, "path": sprint_path, "sprint_number": _extract_sprint_number(name),
                             "start_date": _serialize_datetime(start), "finish_date": _serialize_datetime(finish),
                             "goal": None, "goal_available": False,
                             "goal_note": "A API de classificações não expõe a meta da sprint."})
        for child in getattr(node, "children", None) or []:
            collect(child)

    collect(root)
    return sorted(contexts, key=lambda context: (
        _extract_iteration_year(context["path"]) or 0,
        _extract_sprint_number(context["name"]) or 0,
        _is_plain_sprint_name(context["name"]), context["path"],
    ), reverse=True)


def _select_summary_sprint(sprints: list[dict[str, Any]], sprint_path: str | None) -> dict[str, Any]:
    if sprint_path is not None:
        if not isinstance(sprint_path, str) or not sprint_path.strip():
            raise ValueError("O caminho da sprint selecionada deve ser preenchido.")
        selected = next((sprint for sprint in sprints if sprint["path"] == sprint_path), None)
        if selected is None:
            raise ValueError("A sprint selecionada não existe no projeto configurado.")
        return dict(selected)
    current = _get_current_sprint_context()
    candidates = [
        sprint for sprint in sprints
        if _extract_iteration_year(sprint["path"]) == _extract_iteration_year(current.get("path"))
        and _extract_sprint_number(sprint["name"]) == _extract_sprint_number(current.get("name"))
        and _is_plain_sprint_name(sprint["name"])
    ]
    principal = min(candidates, key=lambda sprint: sprint["path"].count("\\"), default=None)
    return dict(principal) if principal else current


def _build_daily_summary(email: str, sprint_path: str | None = None, story_id: int | None = None) -> dict[str, Any]:
    available_sprints = _available_sprint_contexts()
    branch_sprint_path = None
    if story_id is not None:
        if isinstance(story_id, bool) or not isinstance(story_id, int) or story_id <= 0:
            raise ValueError("O ID da história da branch deve ser um inteiro positivo.")
        snapshot = _get_work_item_snapshot(get_client().clients.get_work_item_tracking_client(), story_id)
        if str(snapshot.get("work_item_type") or "").lower() not in {"user story", "history", "história"}:
            raise ValueError("O item identificado na branch não é uma história.")
        branch_sprint_path = snapshot.get("iteration_path")
        if not branch_sprint_path:
            raise ValueError("A história da branch não possui uma sprint associada.")
        _select_summary_sprint(available_sprints, branch_sprint_path)
        if sprint_path is None:
            sprint_path = branch_sprint_path
    sprint_context = _select_summary_sprint(available_sprints, sprint_path)
    resolved_email, email_source, env_file_path = _resolve_active_user_identity()
    user_name = _resolve_active_user_name(email)
    items = _query_assigned_items(email, sprint_path=sprint_context.get("path"))
    blocked_items = [item for item in items if item["is_blocked"]]
    story_items = [item for item in items if _is_story_type(item.get("work_item_type"))]
    task_items = [item for item in items if _is_task_type(item.get("work_item_type"))]
    assigned_item_type_breakdown = _count_items_by_type(items)
    task_state_breakdown = _count_items_by_state(task_items)
    child_items_summary = _query_child_items_summary(items)
    assigned_story_points = sum(float(item["story_points"] or 0) for item in items)
    remaining_work_hours = sum(float(item["remaining_work_hours"] or 0) for item in items)
    sprint_stories = _query_sprint_stories(sprint_path=sprint_context.get("path"))
    project_progress = _build_project_progress(sprint_stories)

    open_prs: list[dict[str, Any]] = []
    connection = get_client()
    git = connection.clients.get_git_client()
    repos = git.get_repositories(project=get_project())
    for repo in repos:
        prs = git.get_pull_requests(
            repository_id=repo.id,
            search_criteria=get_active_pr_search_criteria(),
        )
        for pr in prs:
            creator_email = getattr(pr.created_by, "unique_name", "")
            if email.lower() not in creator_email.lower():
                continue

            age_days = get_age_days(pr.creation_date)
            open_prs.append({
                "pull_request_id": pr.pull_request_id,
                "title": pr.title,
                "repository": repo.name,
                "source_branch": pr.source_ref_name,
                "target_branch": pr.target_ref_name,
                "age_days": age_days,
            })

    return {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "project": get_project(),
        "user_email": email,
        "user_name": user_name,
        "user_identity_context": {
            "resolved_email": resolved_email,
            "email_source": email_source,
            "env_file_path": env_file_path,
        },
        "counts": {
            "assigned_items": len(items),
            "blocked_items": len(blocked_items),
            "open_prs": len(open_prs),
            "user_stories": len(story_items),
            "tasks_all_statuses": len(task_items),
            "tasks_by_state": task_state_breakdown,
            "assigned_items_by_type": assigned_item_type_breakdown,
            "children_of_assigned_items": child_items_summary["total_children"],
            "blocked_children_of_assigned_items": child_items_summary["blocked_children"],
            "sprint_stories": len(sprint_stories),
        },
        "remaining_story_points": project_progress["remaining_story_points"],
        "assigned_story_points": round(assigned_story_points, 2),
        "remaining_work_hours": remaining_work_hours,
        "sprint_context": sprint_context,
        "available_sprints": available_sprints,
        "branch_sprint_path": branch_sprint_path,
        "branch_story_id": story_id,
        "project_progress": project_progress,
        "child_items_summary": child_items_summary,
        "sprint_stories": sprint_stories,
        "items": items,
        "blocked_items": blocked_items,
        "open_prs": open_prs,
    }

def _get_active_user() -> str:
    """Le o usuario ativo da configuracao local e devolve nome amigavel e email."""
    email = _resolve_active_user_email()
    if email:
        name = _resolve_active_user_name(email)
        if name:
            return f"Usuario ativo: {name} <{email}>"
        return f"Usuario ativo: {email}"
    return "Email nao configurado. Configure AZURE_USER_EMAIL no .env ou git config --global user.email seu@email.com"


def _get_my_work_items() -> str:
    email = _resolve_active_user_email()
    if not email:
        return "Email nao configurado. Configure AZURE_USER_EMAIL no .env ou git config --global user.email seu@email.com"

    payload = _build_daily_summary(email)
    payload.pop("open_prs", None)
    return _format_json(payload)


def _get_my_blocked_items() -> str:
    email = _resolve_active_user_email()
    if not email:
        return "Email nao configurado. Configure AZURE_USER_EMAIL no .env ou git config --global user.email seu@email.com"

    payload = _build_daily_summary(email)
    return _format_json({
        "generated_at": payload["generated_at"],
        "project": payload["project"],
        "user_email": payload["user_email"],
        "blocked_count": payload["counts"]["blocked_items"],
        "blocked_items": payload["blocked_items"],
    })


def _get_my_daily_summary(sprint_path: str | None = None, story_id: int | None = None) -> str:
    email = _resolve_active_user_email()
    if not email:
        return "Email nao configurado. Configure AZURE_USER_EMAIL no .env ou git config --global user.email seu@email.com"

    return _format_json(_build_daily_summary(email, sprint_path=sprint_path, story_id=story_id))


def _identity_matches(value: IdentityRef | dict[str, object] | str | None, email: str) -> bool:
    unique_name = value.get("uniqueName") if isinstance(value, dict) else getattr(value, "unique_name", None)
    return str(unique_name or "").lower() == email.lower()


def _serialize_report_item(item: WorkItem, email: str) -> dict[str, Any]:
    fields = _work_item_fields(item)
    state = fields.get("System.State")
    return {
        "id": fields.get("System.Id"),
        "title": fields.get("System.Title"),
        "state": state,
        "work_item_type": fields.get("System.WorkItemType"),
        "iteration_path": fields.get("System.IterationPath"),
        "story_points": fields.get("Microsoft.VSTS.Scheduling.StoryPoints") or 0,
        "assigned_to": _serialize_assigned_to(fields.get("System.AssignedTo")),
        "assigned_to_user": _identity_matches(fields.get("System.AssignedTo"), email),
        "last_changed_by_user": _identity_matches(fields.get("System.ChangedBy"), email),
        "changed_at": str(fields.get("System.ChangedDate", "")),
        "is_completed": _is_completed_state(state),
    }


def _parent_id_of(work_item: WorkItem) -> int | None:
    for relation in getattr(work_item, "relations", None) or []:
        if getattr(relation, "rel", "") == "System.LinkTypes.Hierarchy-Reverse":
            parent_id = str(getattr(relation, "url", "")).rstrip("/").split("/")[-1]
            if parent_id.isdigit():
                return int(parent_id)
    return None


def _build_sprint_work_report(email: str, sprint_path: str | None = None) -> dict[str, Any]:
    sprint = _select_summary_sprint(_available_sprint_contexts(), sprint_path)
    wit = get_client().clients.get_work_item_tracking_client()
    escaped_email = email.replace("'", "''")
    iteration = sprint["path"].replace("'", "''")
    query = Wiql(query=f"""
        SELECT [System.Id]
        FROM WorkItems
        WHERE [System.IterationPath] = '{iteration}'
          AND [System.State] <> 'Removed'
          AND ([System.AssignedTo] = '{escaped_email}' OR [System.ChangedBy] = '{escaped_email}')
        ORDER BY [System.ChangedDate] DESC
    """)
    result = wit.query_by_wiql(query, team_context=get_team_context())
    fields = [
        "System.Id", "System.Title", "System.State", "System.WorkItemType", "System.IterationPath",
        "System.AssignedTo", "System.ChangedBy", "System.ChangedDate",
        "Microsoft.VSTS.Scheduling.StoryPoints",
    ]
    touched = _get_work_items_in_batches(wit, ids=[str(w.id) for w in result.work_items or []], fields=fields) \
        if result.work_items else []

    stories: dict[int, dict[str, Any]] = {}
    orphan_items: list[dict[str, Any]] = []
    for work_item in touched:
        serialized = _serialize_report_item(work_item, email)
        if _is_story_type(serialized["work_item_type"]):
            stories.setdefault(serialized["id"], {**serialized, "tasks": []})
    pending_parents: dict[int, list[dict[str, Any]]] = {}
    for work_item in touched:
        serialized = _serialize_report_item(work_item, email)
        if _is_story_type(serialized["work_item_type"]):
            continue
        parent_id = _parent_id_of(wit.get_work_item(id=serialized["id"], expand="Relations"))
        if parent_id is None:
            orphan_items.append(serialized)
        elif parent_id in stories:
            stories[parent_id]["tasks"].append(serialized)
        else:
            pending_parents.setdefault(parent_id, []).append(serialized)

    if pending_parents:
        for parent in _get_work_items_in_batches(wit, ids=[str(pid) for pid in pending_parents], fields=fields):
            serialized = _serialize_report_item(parent, email)
            stories[serialized["id"]] = {**serialized, "tasks": pending_parents.pop(serialized["id"])}
        for leftover in pending_parents.values():
            orphan_items.extend(leftover)

    # Um item que é filho de outro contêiner aparece aninhado, não duplicado no topo.
    nested_ids = {task["id"] for story in stories.values() for task in story["tasks"]}
    for story in stories.values():
        for task in story["tasks"]:
            if task["id"] in stories:
                task["tasks"] = stories[task["id"]]["tasks"]
    story_list = sorted((s for s in stories.values() if s["id"] not in nested_ids), key=lambda story: story["id"])

    def count_leaves(entries: list[dict[str, Any]]) -> int:
        return sum(count_leaves(entry["tasks"]) if "tasks" in entry else 1 for entry in entries)

    task_total = sum(count_leaves(story["tasks"]) for story in story_list) + len(orphan_items)
    return {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "project": get_project(),
        "user_email": email,
        "sprint_context": sprint,
        "criteria": "Itens da sprint atribuídos ao usuário ou cuja última alteração foi feita por ele.",
        "counts": {"stories": len(story_list), "tasks": task_total, "orphan_items": len(orphan_items)},
        "stories": story_list,
        "orphan_items": orphan_items,
    }


def _get_sprint_work_report(sprint_path: str | None = None, email: str | None = None) -> str:
    resolved = email or _resolve_active_user_email()
    if not resolved:
        return "Email nao configurado. Configure AZURE_USER_EMAIL no .env ou git config --global user.email seu@email.com"
    return _format_json(_build_sprint_work_report(resolved, sprint_path=sprint_path))


def _get_user_stories(email: str) -> str:
    """Busca historias atribuidas ao email no Azure DevOps."""
    connection = get_client()
    wit = connection.clients.get_work_item_tracking_client()
    escaped_email = email.replace("'", "''")

    query = Wiql(query=f"""
        SELECT [System.Id], [System.Title], [System.State],
               [Microsoft.VSTS.Scheduling.StoryPoints], [System.IterationPath]
        FROM WorkItems
        WHERE [System.AssignedTo] = '{escaped_email}'
          AND [System.WorkItemType] = 'User Story'
          AND [System.State] NOT IN ('Closed', 'Resolved', 'Done')
        ORDER BY [Microsoft.VSTS.Common.Priority] ASC
    """)

    result = wit.query_by_wiql(query, team_context=get_team_context())
    if not result.work_items:
        return "Nenhuma historia encontrada para este usuario."

    ids = [str(work_item.id) for work_item in result.work_items]
    items = _get_work_items_in_batches(wit, ids=ids, fields=[
        "System.Id", "System.Title", "System.State",
        "Microsoft.VSTS.Scheduling.StoryPoints", "System.IterationPath"
    ])

    lines = ["Historias atribuidas:\n"]
    for item in items:
        fields = _work_item_fields(item)
        lines.append(
            f"KL-{fields['System.Id']}: {fields['System.Title']}\n"
            f"  Status: {fields['System.State']} | "
            f"Story Points: {fields.get('Microsoft.VSTS.Scheduling.StoryPoints', '?')} | "
            f"Sprint: {fields.get('System.IterationPath', '?')}\n"
        )
    return "\n".join(lines)


def _get_sprint_stories() -> str:
    """Busca historias do sprint atual com progresso agregado do projeto."""
    sprint_stories = _query_sprint_stories(include_details=False)
    return _format_json(
        _build_sprint_stories_payload(
            sprint_stories,
            detail_mode="light",
            sprint_context=_get_current_sprint_context(),
        )
    )


def _get_sprint_stories_detailed() -> str:
    """Busca historias do sprint atual com description e acceptance criteria."""
    sprint_stories = _query_sprint_stories(include_details=True)
    return _format_json(
        _build_sprint_stories_payload(
            sprint_stories,
            detail_mode="detailed",
            sprint_context=_get_current_sprint_context(),
        )
    )


def _get_story_details(story_id: int) -> str:
    """Retorna detalhes completos da historia incluindo criterios de aceite e tasks."""
    connection = get_client()
    wit = connection.clients.get_work_item_tracking_client()

    item = wit.get_work_item(id=story_id, expand="Relations")

    fields = _work_item_fields(item)
    assigned = fields.get("System.AssignedTo") or {}
    assigned_email = assigned.get("uniqueName") if isinstance(assigned, dict) else getattr(assigned, "unique_name", None)
    parent_ids = [
        int(str(relation.url).rstrip("/").rsplit("/", 1)[-1])
        for relation in getattr(item, "relations", None) or []
        if getattr(relation, "rel", None) == "System.LinkTypes.Hierarchy-Reverse"
    ]
    children: list[dict[str, Any]] = []
    lines = [
        f"=== KL-{story_id}: {fields['System.Title']} ===",
        f"Status: {fields['System.State']}",
        f"Story Points: {fields.get('Microsoft.VSTS.Scheduling.StoryPoints', '?')}",
        f"Prioridade: {fields.get('Microsoft.VSTS.Common.Priority', '?')}",
        f"Sprint: {fields.get('System.IterationPath', '?')}",
        "",
        "Descricao:",
        fields.get("System.Description", "Sem descricao"),
        "",
        "Criterios de Aceite:",
        fields.get("Microsoft.VSTS.Common.AcceptanceCriteria", "Sem criterios definidos"),
        "",
    ]

    if item.relations:
        task_ids = [
            rel.url.split("/")[-1]
            for rel in item.relations
            if rel.rel == "System.LinkTypes.Hierarchy-Forward"
        ]
        if task_ids:
            tasks = _get_work_items_in_batches(wit, ids=task_ids, fields=[
                "System.Id", "System.Title", "System.State", "System.WorkItemType",
                "System.AssignedTo", "System.AreaPath", "System.IterationPath",
                "Microsoft.VSTS.Scheduling.StoryPoints",
                "Microsoft.VSTS.Scheduling.RemainingWork",
                "Microsoft.VSTS.Scheduling.CompletedWork",
                "Microsoft.VSTS.Scheduling.OriginalEstimate",
            ])
            lines.append("Tasks:")
            for task in tasks:
                task_fields = _work_item_fields(task)
                children.append(json.loads(_serialize_work_item_payload(task)))
                lines.append(
                    f"  - [{task_fields['System.State']}] {task_fields['System.Title']} "
                    f"(horas restantes: {task_fields.get('Microsoft.VSTS.Scheduling.RemainingWork', '?')})"
                )

    return _serialize_work_item_payload(item, {
        "story_id": story_id,
        "assigned_to_email": assigned_email,
        "activated_at": str(fields.get("Microsoft.VSTS.Common.ActivatedDate") or "") or None,
        "parent_id": parent_ids[0] if len(parent_ids) == 1 else None,
        "description": fields.get("System.Description"),
        "acceptance_criteria": fields.get("Microsoft.VSTS.Common.AcceptanceCriteria"),
        "priority": fields.get("Microsoft.VSTS.Common.Priority"),
        "tags": fields.get("System.Tags"),
        "child_work_items": children,
        "message": "\n".join(lines),
    })


def _get_open_prs(email: str) -> str:
    """Lista PRs abertas criadas pelo usuario."""
    connection = get_client()
    git = connection.clients.get_git_client()

    repos = git.get_repositories(project=get_project())
    open_prs = []

    for repo in repos:
        prs = git.get_pull_requests(
            repository_id=repo.id,
            search_criteria=get_active_pr_search_criteria()
        )
        for pr in prs:
            creator_email = getattr(pr.created_by, "unique_name", "")
            if email.lower() in creator_email.lower():
                age_days = get_age_days(pr.creation_date)
                open_prs.append(
                    f"PR #{pr.pull_request_id}: {pr.title}\n"
                    f"  Repo: {repo.name} | Branch: {pr.source_ref_name} -> {pr.target_ref_name}\n"
                    f"  Criada ha {age_days} dias"
                )

    if not open_prs:
        return "Nenhuma PR aberta encontrada para este usuario."
    return "PRs abertas:\n\n" + "\n\n".join(open_prs)


def _preview_story_branch_association(
    story_id: int,
    repository_name: str,
    source_branch: str,
    related_work_item_ids: list[int] | None = None,
    include_child_work_items: bool = True,
) -> str:
    connection = get_client()
    git = connection.clients.get_git_client()
    wit = connection.clients.get_work_item_tracking_client()
    repository = _resolve_git_repository(git, repository_name)
    preview_payload = _build_story_branch_association_preview_payload(
        wit,
        repository=repository,
        story_id=story_id,
        source_branch=source_branch,
        related_work_item_ids=related_work_item_ids,
        include_child_work_items=include_child_work_items,
    )
    return _format_json(preview_payload)


def _associate_story_branch(
    story_id: int,
    repository_name: str,
    source_branch: str,
    related_work_item_ids: list[int] | None = None,
    include_child_work_items: bool = True,
) -> str:
    connection = get_client()
    git = connection.clients.get_git_client()
    wit = connection.clients.get_work_item_tracking_client()
    repository = _resolve_git_repository(git, repository_name)
    preview_payload = _build_story_branch_association_preview_payload(
        wit,
        repository=repository,
        story_id=story_id,
        source_branch=source_branch,
        related_work_item_ids=related_work_item_ids,
        include_child_work_items=include_child_work_items,
    )
    _check_association_revisions(wit, preview_payload["work_items"])
    association_result = _associate_artifact_to_work_items(
        wit,
        work_items=preview_payload["work_items"],
        artifact_url=preview_payload["branch_artifact_uri"],
        artifact_name="Branch",
    )
    preview_payload.update(
        {
            "executed": True,
            "association_result": association_result,
            "expected_revisions": {str(key): value for key, value in EXPECTED_REVISIONS.get().items()},
        }
    )
    return _format_json(preview_payload)


def _preview_story_pull_request(
    story_id: int,
    repository_name: str,
    source_branch: str,
    what_was_changed: str,
    affected_processes: str,
    expected_impacts: str,
    important_points: str,
    mermaid_diagram: str,
    related_work_item_ids: list[int] | None = None,
    include_child_work_items: bool = True,
    target_branch: str | None = None,
    title: str | None = None,
    tests_updated: bool = False,
    tests_unchanged: bool = False,
    new_library_name: str | None = None,
    env_changes: str | None = None,
    generated_migration_or_seed: str | None = None,
    new_queue_or_command: str | None = None,
) -> str:
    connection = get_client()
    git = connection.clients.get_git_client()
    wit = connection.clients.get_work_item_tracking_client()
    repository = _resolve_git_repository(git, repository_name)
    preview_payload = _build_story_pull_request_preview_payload(
        wit,
        git=git,
        repository=repository,
        story_id=story_id,
        source_branch=source_branch,
        related_work_item_ids=related_work_item_ids,
        include_child_work_items=include_child_work_items,
        target_branch=target_branch,
        title=title,
        what_was_changed=what_was_changed,
        affected_processes=affected_processes,
        expected_impacts=expected_impacts,
        important_points=important_points,
        tests_updated=tests_updated,
        tests_unchanged=tests_unchanged,
        new_library_name=new_library_name,
        env_changes=env_changes,
        generated_migration_or_seed=generated_migration_or_seed,
        new_queue_or_command=new_queue_or_command,
        mermaid_diagram=mermaid_diagram,
    )
    return _format_json(preview_payload)


def _create_story_pull_request(
    story_id: int,
    repository_name: str,
    source_branch: str,
    what_was_changed: str,
    affected_processes: str,
    expected_impacts: str,
    important_points: str,
    mermaid_diagram: str,
    related_work_item_ids: list[int] | None = None,
    include_child_work_items: bool = True,
    target_branch: str | None = None,
    title: str | None = None,
    tests_updated: bool = False,
    tests_unchanged: bool = False,
    new_library_name: str | None = None,
    env_changes: str | None = None,
    generated_migration_or_seed: str | None = None,
    new_queue_or_command: str | None = None,
) -> str:
    connection = get_client()
    git = connection.clients.get_git_client()
    wit = connection.clients.get_work_item_tracking_client()
    repository = _resolve_git_repository(git, repository_name)
    preview_payload = _build_story_pull_request_preview_payload(
        wit,
        git=git,
        repository=repository,
        story_id=story_id,
        source_branch=source_branch,
        related_work_item_ids=related_work_item_ids,
        include_child_work_items=include_child_work_items,
        target_branch=target_branch,
        title=title,
        what_was_changed=what_was_changed,
        affected_processes=affected_processes,
        expected_impacts=expected_impacts,
        important_points=important_points,
        tests_updated=tests_updated,
        tests_unchanged=tests_unchanged,
        new_library_name=new_library_name,
        env_changes=env_changes,
        generated_migration_or_seed=generated_migration_or_seed,
        new_queue_or_command=new_queue_or_command,
        mermaid_diagram=mermaid_diagram,
    )

    if preview_payload.get("existing_active_pull_request") is not None:
        raise ValueError("existing_active_pull_request: a nova PR aprovada nao pode reutilizar uma PR ativa existente.")

    _check_association_revisions(wit, preview_payload["branch_association_preview"]["work_items"])
    branch_association_result = _associate_artifact_to_work_items(
        wit,
        work_items=preview_payload["branch_association_preview"]["work_items"],
        artifact_url=preview_payload["branch_association_preview"]["branch_artifact_uri"],
        artifact_name="Branch",
    )

    _check_association_revisions(wit, preview_payload["branch_association_preview"]["work_items"])
    created_pull_request = git.create_pull_request(
        GitPullRequest(
            source_ref_name=preview_payload["source_ref_name"],
            target_ref_name=preview_payload["target_ref_name"],
            title=preview_payload["title"],
            description=preview_payload["body_markdown"],
            is_draft=True,
        ),
        repository_id=preview_payload["repository_id"],
        project=get_project(),
        supports_iterations=True,
    )
    pull_request_summary = _build_pull_request_summary(
        created_pull_request,
        preview_payload["repository_name"],
    )

    if pull_request_summary is None or pull_request_summary.get("pull_request_id") is None:
        raise ValueError("Nao foi possivel resolver a PR da historia apos a tentativa de criacao.")

    pull_request_artifact_id = (
        pull_request_summary.get("artifact_id")
        or _build_pull_request_artifact_uri(
            preview_payload["project_id"],
            preview_payload["repository_id"],
            int(pull_request_summary["pull_request_id"]),
        )
    )
    pull_request_summary["artifact_id"] = pull_request_artifact_id

    pull_request_association_result = _associate_artifact_to_work_items(
        wit,
        work_items=preview_payload["branch_association_preview"]["work_items"],
        artifact_url=pull_request_artifact_id,
        artifact_name="Pull Request",
    )

    try:
        work_item_refs = git.get_pull_request_work_item_refs(
            repository_id=preview_payload["repository_id"],
            pull_request_id=int(pull_request_summary["pull_request_id"]),
            project=get_project(),
        )
        pull_request_summary["work_item_refs"] = [
            {
                "id": getattr(work_item_ref, "id", None),
                "url": getattr(work_item_ref, "url", None),
            }
            for work_item_ref in (work_item_refs or [])
        ]
    except Exception:
        pull_request_summary["work_item_refs"] = None

    preview_payload.update(
        {
            "executed": True,
            "created": True,
            "pull_request": pull_request_summary,
            "branch_association_result": branch_association_result,
            "pull_request_association_result": pull_request_association_result,
            "expected_revisions": {str(key): value for key, value in EXPECTED_REVISIONS.get().items()},
            "message": "PR Draft criada com sucesso e link retornado ao usuario.",
        }
    )
    return _format_json(preview_payload)


def _create_work_item(
    work_item_type: str,
    title: str,
    description: str | None = None,
    repository_name: str | None = None,
    iteration_path: str | None = None,
    use_next_sprint: bool = False,
    area_path: str | None = None,
    assigned_to: str | None = None,
    tags: str | None = None,
    parent_work_item_id: int | None = None,
    acceptance_criteria: str | None = None,
    story_points: float | None = None,
    remaining_work_hours: float | None = None,
    completed_work_hours: float | None = None,
    original_estimate_hours: float | None = None,
) -> str:
    normalized_work_item_type = str(work_item_type or "").strip()
    normalized_title = str(title or "").strip()

    if not normalized_work_item_type:
        raise ValueError("work_item_type e obrigatorio para criar um work item.")
    if not normalized_title:
        raise ValueError("title e obrigatorio para criar um work item.")

    resolved_repository_name = _infer_repository_name(repository_name, normalized_title, description)
    formatted_description = _format_azure_rich_text(description)
    formatted_acceptance_criteria = _format_azure_rich_text(acceptance_criteria)

    next_sprint_context = None
    target_iteration_path = str(iteration_path or "").strip()
    if not target_iteration_path and use_next_sprint:
        next_sprint_context = _get_next_sprint_context()
        target_iteration_path = str(next_sprint_context.get("path") or "").strip()

    connection = get_client()
    wit = connection.clients.get_work_item_tracking_client()

    if not target_iteration_path and not use_next_sprint and parent_work_item_id:
        parent_item = wit.get_work_item(id=parent_work_item_id, fields=["System.IterationPath"])
        target_iteration_path = str(parent_item.fields.get("System.IterationPath") or "").strip()

    resolved_work_item_type = _resolve_work_item_type_name(wit, normalized_work_item_type)
    supported_fields = _get_supported_work_item_fields(wit, resolved_work_item_type)

    document: list[dict[str, Any]] = []
    applied_fields: list[str] = []
    skipped_fields: list[str] = []
    _append_work_item_field_patch(document, "System.Title", normalized_title)
    _append_work_item_field_patch(document, "System.Description", formatted_description)
    _append_work_item_field_patch(document, "System.IterationPath", target_iteration_path)
    _append_work_item_field_patch(document, "System.AreaPath", area_path)
    _append_work_item_field_patch(document, "System.AssignedTo", assigned_to)
    _append_work_item_field_patch(document, "System.Tags", tags)
    _append_parent_relation_patch(document, parent_work_item_id)
    _append_supported_work_item_field_patch(
        document,
        supported_fields,
        "Microsoft.VSTS.Common.AcceptanceCriteria",
        formatted_acceptance_criteria,
        applied_fields,
        skipped_fields,
    )
    _append_supported_work_item_field_patch(
        document,
        supported_fields,
        "Microsoft.VSTS.Scheduling.StoryPoints",
        story_points,
        applied_fields,
        skipped_fields,
    )
    _append_supported_work_item_field_patch(
        document,
        supported_fields,
        "Microsoft.VSTS.Scheduling.RemainingWork",
        remaining_work_hours,
        applied_fields,
        skipped_fields,
    )
    _append_supported_work_item_field_patch(
        document,
        supported_fields,
        "Microsoft.VSTS.Scheduling.CompletedWork",
        completed_work_hours,
        applied_fields,
        skipped_fields,
    )
    _append_supported_work_item_field_patch(
        document,
        supported_fields,
        "Microsoft.VSTS.Scheduling.OriginalEstimate",
        original_estimate_hours,
        applied_fields,
        skipped_fields,
    )

    created_item = wit.create_work_item(
        document=document,
        project=get_project(),
        type=resolved_work_item_type,
    )

    return _serialize_work_item_payload(
        created_item,
        {
            "requested_work_item_type": normalized_work_item_type,
            "resolved_work_item_type": resolved_work_item_type,
            "repository_name": resolved_repository_name,
            "requested_iteration_path": str(iteration_path or "").strip() or None,
            "resolved_iteration_path": target_iteration_path or None,
            "used_next_sprint": bool(next_sprint_context),
            "next_sprint_context": next_sprint_context,
            "parent_work_item_id": parent_work_item_id,
            "requested_effort": _build_effort_payload(
                story_points=story_points,
                remaining_work_hours=remaining_work_hours,
                completed_work_hours=completed_work_hours,
                original_estimate_hours=original_estimate_hours,
            ),
            "applied_fields": applied_fields,
            "skipped_fields": skipped_fields,
            "local_context_path": None,
            "similarity_analysis": None,
            "context_persistence": "moved_to_langgraph",
            "similarity_analysis_owner": "langgraph",
        },
    )


def _update_work_item_content(
    work_item_id: int,
    description: str | None = None,
    acceptance_criteria: str | None = None,
    comment: str | None = None,
    assigned_to: str | None = None,
    area_path: str | None = None,
    status: str | None = None,
) -> str:
    normalized_comment = str(comment or "").strip()
    formatted_description = _format_azure_rich_text(description) if description is not None else None
    formatted_acceptance_criteria = (
        _format_azure_rich_text(acceptance_criteria) if acceptance_criteria is not None else None
    )

    if all(value is None for value in (description, acceptance_criteria, assigned_to, area_path, status)) and not normalized_comment:
        raise ValueError(
            "Informe description, acceptance_criteria ou comment para atualizar o conteudo do work item."
        )

    connection = get_client()
    wit = connection.clients.get_work_item_tracking_client()
    current_item = wit.get_work_item(id=work_item_id, fields=[
        "System.Id",
        "System.Title",
        "System.State",
        "System.WorkItemType",
        "System.AssignedTo",
        "System.IterationPath",
    ])
    work_item_type = str(current_item.fields.get("System.WorkItemType") or "").strip()
    supported_fields = _get_supported_work_item_fields(wit, work_item_type)

    document: list[dict[str, Any]] = []
    applied_fields: list[str] = []
    skipped_fields: list[str] = []
    requested_fields: list[str] = []

    for name, reference, value in (
        ("assigned_to", "System.AssignedTo", assigned_to),
        ("area_path", "System.AreaPath", area_path),
        ("status", "System.State", status),
    ):
        if value is not None:
            if reference not in supported_fields:
                raise ValueError(f"{reference} is not supported by {work_item_type}.")
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string.")
            requested_fields.append(name)
            document.append({"op": "add", "path": f"/fields/{reference}", "value": value})
            applied_fields.append(name)

    if description is not None:
        requested_fields.append("description")
        _append_work_item_field_patch(document, "System.Description", formatted_description)
        if formatted_description:
            applied_fields.append("description")

    if acceptance_criteria is not None:
        requested_fields.append("acceptance_criteria")
        _append_supported_work_item_field_patch(
            document,
            supported_fields,
            "Microsoft.VSTS.Common.AcceptanceCriteria",
            formatted_acceptance_criteria,
            applied_fields,
            skipped_fields,
        )

    if normalized_comment:
        document.append({"op": "add", "path": "/fields/System.History", "value": normalized_comment})

    if not document:
        raise ValueError(
            "Nenhum campo de conteudo resultou em atualizacao. "
            f"Campos solicitados: {', '.join(requested_fields) or 'nenhum'}. "
            f"Campos ignorados: {', '.join(skipped_fields) or 'nenhum'}"
        )

    updated_item = update_work_item(wit, document=document, id=work_item_id)
    _confirm_work_item_status(updated_item, work_item_id, status)
    return _serialize_work_item_payload(
        updated_item,
        {
            "requested_fields": requested_fields,
            "applied_fields": applied_fields,
            "skipped_fields": skipped_fields,
            "comment_added": bool(normalized_comment),
            "description_format": "azure_simple_html" if description is not None else None,
            "acceptance_criteria_format": "azure_simple_html" if acceptance_criteria is not None else None,
        },
    )


def _update_work_item_effort(
    work_item_id: int,
    story_points: float | None = None,
    remaining_work_hours: float | None = None,
    completed_work_hours: float | None = None,
    original_estimate_hours: float | None = None,
    comment: str | None = None,
    status: str | None = None,
) -> str:
    requested_effort = _build_effort_payload(
        story_points=story_points,
        remaining_work_hours=remaining_work_hours,
        completed_work_hours=completed_work_hours,
        original_estimate_hours=original_estimate_hours,
    )
    normalized_comment = str(comment or "").strip()

    if not requested_effort:
        raise ValueError(
            "Informe ao menos um campo de esforco para atualizar: story_points, "
            "remaining_work_hours, completed_work_hours ou original_estimate_hours."
        )

    connection = get_client()
    wit = connection.clients.get_work_item_tracking_client()
    current_item = wit.get_work_item(id=work_item_id, fields=[
        "System.Id",
        "System.Title",
        "System.State",
        "System.WorkItemType",
        "System.AssignedTo",
        "System.IterationPath",
    ])
    work_item_type = str(current_item.fields.get("System.WorkItemType") or "").strip()
    supported_fields = _get_supported_work_item_fields(wit, work_item_type)

    document: list[dict[str, Any]] = []
    applied_fields: list[str] = []
    skipped_fields: list[str] = []
    _append_supported_work_item_field_patch(
        document,
        supported_fields,
        "Microsoft.VSTS.Scheduling.StoryPoints",
        story_points,
        applied_fields,
        skipped_fields,
    )
    _append_supported_work_item_field_patch(
        document,
        supported_fields,
        "Microsoft.VSTS.Scheduling.RemainingWork",
        remaining_work_hours,
        applied_fields,
        skipped_fields,
    )
    _append_supported_work_item_field_patch(
        document,
        supported_fields,
        "Microsoft.VSTS.Scheduling.CompletedWork",
        completed_work_hours,
        applied_fields,
        skipped_fields,
    )
    _append_supported_work_item_field_patch(
        document,
        supported_fields,
        "Microsoft.VSTS.Scheduling.OriginalEstimate",
        original_estimate_hours,
        applied_fields,
        skipped_fields,
    )

    if status is not None:
        if "System.State" not in supported_fields:
            raise ValueError(f"System.State is not supported by {work_item_type}.")
        if not isinstance(status, str) or not status.strip():
            raise ValueError("status must be a non-empty string.")
        if not applied_fields or skipped_fields:
            raise ValueError("No requested effort field may be skipped for the atomic update.")
        document.append({"op": "add", "path": "/fields/System.State", "value": status})
        applied_fields.append("status")

    if normalized_comment:
        document.append({"op": "add", "path": "/fields/System.History", "value": normalized_comment})

    if not document:
        raise ValueError(
            f"Nenhum dos campos solicitados e suportado pelo tipo {work_item_type}. "
            f"Campos ignorados: {', '.join(skipped_fields) or 'nenhum'}"
        )

    updated_item = update_work_item(wit, document=document, id=work_item_id)
    _confirm_work_item_status(updated_item, work_item_id, status)
    return _serialize_work_item_payload(
        updated_item,
        {
            "requested_effort": requested_effort,
            "applied_fields": applied_fields,
            "skipped_fields": skipped_fields,
            "comment_added": bool(normalized_comment),
        },
    )


def _move_work_item_to_next_sprint(work_item_id: int, comment: str | None = None) -> str:
    next_sprint_context = _get_next_sprint_context()
    target_iteration_path = str(next_sprint_context.get("path") or "").strip()

    connection = get_client()
    wit = connection.clients.get_work_item_tracking_client()
    current_item = wit.get_work_item(id=work_item_id, fields=[
        "System.Id",
        "System.Title",
        "System.State",
        "System.WorkItemType",
        "System.AssignedTo",
        "System.IterationPath",
    ])
    current_fields = _work_item_fields(current_item)
    previous_iteration_path = str(current_fields.get("System.IterationPath") or "").strip()

    if previous_iteration_path == target_iteration_path:
        return _serialize_work_item_payload(
            current_item,
            {
                "previous_iteration_path": previous_iteration_path,
                "resolved_iteration_path": target_iteration_path,
                "moved": False,
                "next_sprint_context": next_sprint_context,
                "message": "O work item ja esta na proxima sprint configurada.",
            },
        )

    document = [{"op": "add", "path": "/fields/System.IterationPath", "value": target_iteration_path}]
    normalized_comment = str(comment or "").strip()
    if normalized_comment:
        document.append({"op": "add", "path": "/fields/System.History", "value": normalized_comment})

    updated_item = update_work_item(wit, document=document, id=work_item_id)
    return _serialize_work_item_payload(
        updated_item,
        {
            "previous_iteration_path": previous_iteration_path or None,
            "resolved_iteration_path": target_iteration_path,
            "moved": True,
            "next_sprint_context": next_sprint_context,
            "comment_added": bool(normalized_comment),
        },
    )


def _update_story_status(story_id: int, status: str) -> str:
    """Atualiza o estado de uma historia."""
    connection = get_client()
    wit = connection.clients.get_work_item_tracking_client()

    patch = [{"op": "add", "path": "/fields/System.State", "value": status}]
    updated_item = update_work_item(wit, document=patch, id=story_id)
    _confirm_work_item_status(updated_item, story_id, status)
    return _serialize_work_item_payload(updated_item, {
        "story_id": story_id,
        "requested_status": status,
        "status_confirmed": True,
        "message": f"Historia KL-{story_id} atualizada para: {status}",
    })


def _add_story_comment(story_id: int, comment: str) -> str:
    """Adiciona comentario a uma historia."""
    connection = get_client()
    wit = connection.clients.get_work_item_tracking_client()

    patch = [{"op": "add", "path": "/fields/System.History", "value": comment}]
    update_work_item(wit, document=patch, id=story_id)
    return f"Comentario adicionado na historia KL-{story_id}"

