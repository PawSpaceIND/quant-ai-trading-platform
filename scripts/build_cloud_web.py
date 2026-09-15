import json
import os
import shutil
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parents[1]
source = root / "apps/pramana-ui"
target = root / ".cloud-build"
if target.exists():
    shutil.rmtree(target)
shutil.copytree(source, target, ignore=shutil.ignore_patterns("node_modules", ".next", "out", "*.tsbuildinfo", ".env*"))
shutil.rmtree(target / "app/api")
# Private tests import server routes omitted from this static deployment copy.
# The complete source and tests are type-checked and run by the separate UI job.
shutil.rmtree(target / "tests", ignore_errors=True)
(target / "proxy.ts").unlink(missing_ok=True)
shutil.rmtree(target / "app/login", ignore_errors=True)
(target / "node_modules").symlink_to((source / "node_modules").resolve(), target_is_directory=True)
(target / "next.config.ts").write_text('export default {output:"export",images:{unoptimized:true},outputFileTracingRoot:'+json.dumps(str(target))+'};')
subprocess.run(["npm", "run", "build", "--", "--webpack"], cwd=target, check=True, env={**os.environ, "NEXT_PUBLIC_PRAMANA_HOSTED":"true"})
