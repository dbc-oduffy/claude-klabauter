"""Sizing-record guard: staging via a path variable and quoted-script reads are not writes."""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import guard_doctrine_surface_bash_write as g

IDS = g._SIZING_RECORD_IDENTIFIERS
SZ = "state/sizings/x.yaml"


@pytest.mark.parametrize(
    "cmd",
    [
        'P="docs/x.md state/sizings/a.yaml"; git add -- $P && git commit -q -m msg -- $P',
        'P="docs/x.md state/sizings/a.yaml"; git add -- $P && git commit -q -m "tee the sed -i notes" -- $P',
        'P="state/sizings/a.yaml"; git add -- "$P"; git commit -q -m "write notes > tee" -- "$P"',
        f"python3 -c \"import yaml; d=yaml.safe_load(open('{SZ}'))\"",
        f"python3 -c \"import yaml; d=yaml.safe_load(open('{SZ}','rb'))\"",
        f"python3 -c \"import yaml; d=yaml.safe_load(open('{SZ}','r')); print(d['a'] > 1)\"",
        f"python3 -c \"import yaml,sys; sys.stdout.write(str(yaml.safe_load(open('{SZ}'))))\"",
    ],
)
def test_staging_and_reads_are_allowed(cmd):
    assert not g.is_denied_bash_write(cmd, IDS)


@pytest.mark.parametrize(
    "cmd",
    [
        f"echo x > {SZ}",
        f"echo x >> {SZ}",
        f"echo x | tee {SZ}",
        f"sed -i s/a/b/ {SZ}",
        f"cp a.yaml {SZ}",
        f"mv a.yaml {SZ}",
        f"python3 -c \"open('{SZ}','w').write('y')\"",
        f"python3 -c \"open('{SZ}','a').write('y')\"",
        f"python3 -c \"open('{SZ}','x').write('y')\"",
        f"python3 -c \"from pathlib import Path; Path('{SZ}').write_text('y')\"",
        f"python3 -c \"import yaml; d=yaml.safe_load(open('{SZ}'))\" > {SZ}",
        f'P="{SZ}"; echo x > $P',
        f'P="{SZ}"; echo x | tee $P',
        f'P="{SZ}"; git add -- $P; echo x >> $P',
        f"python3 -c \"import sys; sys.stdout.write(open('{SZ}').read()); open('{SZ}','w').write('')\"",
    ],
)
def test_real_writes_still_deny(cmd):
    assert g.is_denied_bash_write(cmd, IDS)
