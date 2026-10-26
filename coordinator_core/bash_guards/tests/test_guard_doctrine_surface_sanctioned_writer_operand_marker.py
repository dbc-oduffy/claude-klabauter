"""A write-marker word inside an unquoted operand of a sanctioned writer is data;
a redirect or a downstream write stage in the same segment still denies."""
import pytest

from coordinator_core.bash_guards.guard_doctrine_surface_bash_write import (
    _sizing_write_denied,
)

S = "state/sizings/2026-10-26-x.yaml"

ALLOWED = [
    f"sizing-assemble --write {S} --name doc-saver-forum-queue-tab-patch",
    f"sizing-assemble --write {S} --name fix-tee-rsync-cp-slug",
    f"coordinator-doc-new --type sizing --name patch-tee {S}",
    f"sizing-assemble --write {S} --name x-patch | tail -n 3",
    f"session-claim-cli release-artifact {S} --owner x-patch",
    f"session-claim-cli clear-claim-if-dead {S} --reason tee-rsync",
    f"coordinator-safe-commit -m x-patch-tee -- {S}",
    f"scoped-git-commit --message cp-patch-fix {S}",
    f"& \"$h/bin/sizing-assemble\" --tshirt S --name x-patch --write '{S}'",
    "$h = if ($env:X) { $env:X } else { 'y' }\n"
    f"& \"$h/bin/sizing-assemble\" --tshirt S --name x-patch --write '{S}'",
]

DENIED = [
    f"sizing-assemble --name x-patch > {S}",
    f"sizing-assemble --name x-tee >> {S}",
    f"sizing-assemble --name x-patch | tee {S}",
    f"sizing-assemble --name x-patch || tee {S}",
    f"patch {S} fix.diff",
    f"rsync a.yaml {S}",
    f"cp a.yaml {S}",
    f"sizing-assemble --name x-patch | cp a.yaml {S}",
    f"sizing-assemble --name x-patch | sed -i s/a/b/ {S}",
    f"session-claim-cli release-artifact {S} --owner x-patch > {S}",
    f"session-claim-cli release-artifact {S} --owner x-patch | tee {S}",
    f"session-claim-cli clear-claim-if-dead {S} >> {S}",
    f"coordinator-safe-commit -m x-patch -- {S} > {S}",
    f"coordinator-safe-commit -m x-patch -- {S} | tee {S}",
    f"scoped-git-commit --message x {S} >> {S}",
    f"& \"$h/bin/sizing-assemble\" --name x-patch > {S}",
    f"& \"$h/bin/sizing-assemble\" --name x-patch | tee {S}",
    f"& \"$h/bin/sizing-assemble\" --name x-patch | Out-File {S}",
    f"& \"$h/bin/sizing-assemble\" --name x-patch | Set-Content {S}",
    f"& patch {S} fix.diff",
    f"& python sizing-assemble.py -c x --write '{S}'",
]


@pytest.mark.parametrize("cmd", ALLOWED)
def test_operand_write_word_of_sanctioned_writer_is_allowed(cmd):
    assert not _sizing_write_denied(cmd)


@pytest.mark.parametrize("cmd", DENIED)
def test_real_write_into_sizings_still_denies(cmd):
    assert _sizing_write_denied(cmd)
