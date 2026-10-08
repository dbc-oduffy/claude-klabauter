"""Builds the three reachability fixture repos (unwired, wired, undecidable) for review.reachability tests.

The base commit holds everything except the capability; the variant's files, a plan and a sizing
object are left uncommitted, as an execute run leaves them.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from coordinator_core.win_portability import no_console_creationflags

VARIANTS = ("unwired", "wired", "undecidable")
PLAN_REL = "docs/plans/2026-10-08-grant-permission.md"
SIZING_REL = "state/sizings/2026-10-08-grant-permission.yaml"

BASE = {
    "package.json": '{\n  "name": "example-stats-repo-fixture",\n  "private": true\n}\n',
    "app/layout.tsx": (
        "import Nav from './nav';\n\n"
        "export default function Layout({ children }: { children: React.ReactNode }) {\n"
        "  return (<html><body><Nav />{children}</body></html>);\n}\n"
    ),
    "app/page.tsx": "export default function Home() {\n  return <main>Stats</main>;\n}\n",
    "app/nav.tsx": (
        "import Link from 'next/link';\n\n"
        "export default function Nav() {\n"
        "  return (<nav><Link href=\"/\">Home</Link></nav>);\n}\n"
    ),
    "components/Table.tsx": "export function Table() {\n  return <table />;\n}\n",
}

GRANT_FN = (
    "export function grantPermission(userId: string, permission: string): boolean {\n"
    "  return userId.length > 0 && permission.length > 0;\n}\n"
)
API_ROUTE = (
    "import { NextResponse } from 'next/server';\n\n"
    "export async function POST(req: Request) {\n"
    "  const { userId, permission } = await req.json();\n"
    "  return NextResponse.json({ ok: Boolean(userId && permission) });\n}\n"
)
FORM = (
    "'use client';\n"
    "import { grantPermission } from '../lib/permissions';\n\n"
    "async function submit() {\n"
    "  if (grantPermission('u1', 'read')) {\n"
    "    await fetch('/api/permissions', { method: 'POST', body: JSON.stringify({ userId: 'u1', permission: 'read' }) });\n"
    "  }\n}\n\n"
    "export default function GrantForm() {\n"
    "  return (<form onSubmit={submit}><button>Grant</button></form>);\n}\n"
)
PAGE = (
    "import GrantForm from '../../../components/GrantForm';\n\n"
    "export default function PermissionsPage() {\n  return <GrantForm />;\n}\n"
)
NAV_WIRED = (
    "import Link from 'next/link';\n\n"
    "export default function Nav() {\n"
    "  return (<nav><Link href=\"/\">Home</Link><Link href=\"/admin/permissions\">Admin</Link></nav>);\n}\n"
)
RUST = "pub fn grant_permission(user: &str, permission: &str) -> bool {\n    !user.is_empty() && !permission.is_empty()\n}\n"

VARIANT_FILES = {
    "unwired": {"lib/permissions.ts": GRANT_FN, "app/api/permissions/route.ts": API_ROUTE},
    "wired": {
        "lib/permissions.ts": GRANT_FN,
        "app/api/permissions/route.ts": API_ROUTE,
        "components/GrantForm.tsx": FORM,
        "app/admin/permissions/page.tsx": PAGE,
        "app/nav.tsx": NAV_WIRED,
    },
    "undecidable": {"src/permissions.rs": RUST},
}


def _plan(writes: list[str]) -> str:
    rows = "\n".join(f"    - {w}" for w in writes)
    return (
        "---\ntitle: \"Grant permissions from the admin UI\"\nstatus: executing\n"
        f"sizing_object: \"{SIZING_REL}\"\n---\n\n## Tasks\n\n```yaml plan-tasks\n"
        "- id: C1\n  title: Grant permission capability\n  writes:\n"
        f"{rows}\n  disposition: open\n```\n"
    )


SIZING = (
    "exit_criterion:\n  statement: An admin can grant a permission from the admin UI.\n"
    "  click_paths:\n    - role: admin\n      steps: [nav, Admin, Permissions, Grant]\n"
)


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid",
         "-c", "commit.gpgsign=false", *args],
        cwd=root, check=True, capture_output=True,
        **no_console_creationflags(),
    )


def build(variant: str, dest: Path) -> Path:
    if variant not in VARIANTS:
        raise ValueError(f"unknown variant {variant!r}; expected one of {VARIANTS}")
    dest.mkdir(parents=True, exist_ok=True)
    if any(dest.iterdir()):
        raise ValueError(f"{dest} is not empty")
    _git(dest, "init", "-q")
    for rel, text in BASE.items():
        _write(dest, rel, text)
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "-m", "base: everything except the capability")
    files = VARIANT_FILES[variant]
    for rel, text in files.items():
        _write(dest, rel, text)
    _write(dest, PLAN_REL, _plan(sorted(files)))
    _write(dest, SIZING_REL, SIZING)
    return dest
