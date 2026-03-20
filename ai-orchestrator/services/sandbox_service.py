"""
sandbox_service.py
==================
Professional Sandbox Execution Service for SINC AI agents.
Handles Docker-based and Host-based (fallback) script execution with strict safety.
"""

import os
import asyncio
import logging
import tempfile
import shutil
from pathlib import Path
from typing import Tuple, Optional

log = logging.getLogger("sandbox-service")

# Preferred sandbox driver
try:
    import docker
    _HAS_DOCKER = True
except ImportError:
    _HAS_DOCKER = False

class SandboxService:
    def __init__(self, workspace: Path, image: str = "python:3.12-slim", allow_host_fallback: bool = False):
        self.workspace = workspace.resolve()
        self.image = image
        self.allow_host_fallback = allow_host_fallback
        self.docker_client: Optional["docker.DockerClient"] = None
        
        if _HAS_DOCKER:
            try:
                self.docker_client = docker.from_env(timeout=5)
            except Exception as e:
                log.warning(f"Failed to initialize Docker client: {e}")
                self.docker_client = None

    def _validate_wdir(self, wdir: str) -> str:
        try:
            resolved = Path(wdir).resolve()
            # Must be inside workspace
            resolved.relative_to(self.workspace)
            return str(resolved)
        except ValueError:
            # Fallback to workspace root if invalid or outside
            return str(self.workspace)

    async def execute(self, script: str, wdir: str = "", timeout: int = 120) -> Tuple[str, str, int]:
        """Execute a script in a safe environment."""
        safe_wdir = self._validate_wdir(wdir)
        
        if self.docker_client:
            return await self._docker_execute(script, safe_wdir, timeout)
            
        if self.allow_host_fallback:
            log.info("Docker unavailable; falling back to host execution.")
            return await self._host_execute(script, safe_wdir, timeout)
            
        return "failed", "Docker sandbox unavailable and host fallback is disabled", -1

    async def _docker_execute(self, script: str, safe_wdir: str, timeout: int) -> Tuple[str, str, int]:
        workspace_str = str(self.workspace)
        
        # Create temporary script in workspace to be visible to container
        with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False, dir=workspace_str) as tf:
            tf.write("#!/bin/bash\nset -euo pipefail\n" + script)
            script_host_path = tf.name
            
        rel_script = Path(script_host_path).relative_to(self.workspace)
        script_container_path = f"/workspace/{rel_script}"
        
        rel_wdir = Path(safe_wdir).relative_to(self.workspace)
        container_wdir = f"/workspace/{rel_wdir}" if str(rel_wdir) != "." else "/workspace"
        
        try:
            def _run():
                raw = self.docker_client.containers.run(
                    image=self.image, 
                    command=["bash", script_container_path],
                    volumes={workspace_str: {"bind": "/workspace", "mode": "rw"}},
                    working_dir=container_wdir, 
                    network_mode="none", 
                    read_only=True,
                    mem_limit="256m", 
                    remove=True, 
                    stdout=True, 
                    stderr=True, 
                    detach=False,
                )
                return "passed", (raw.decode("utf-8", errors="replace"))[:8000], 0
            
            return await asyncio.wait_for(asyncio.to_thread(_run), timeout=timeout)
        except Exception as e:
            return "failed", str(e), -1
        finally:
            if os.path.exists(script_host_path):
                try: os.remove(script_host_path)
                except OSError: pass

    async def _host_execute(self, script: str, safe_wdir: str, timeout: int) -> Tuple[str, str, int]:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as tf:
            tf.write("#!/bin/bash\nset -euo pipefail\n" + script)
            script_path = tf.name
            
        os.chmod(script_path, 0o700)
        shell_path = shutil.which("bash") or shutil.which("sh") or shutil.which("bash.exe")
        
        if not shell_path:
            return "failed", "No shell found for host execution", -1
            
        try:
            proc = await asyncio.create_subprocess_exec(
                shell_path, script_path, cwd=safe_wdir,
                stdout=asyncio.subprocess.PIPE, 
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            rc = proc.returncode or 0
            output = (stdout + stderr).decode("utf-8", errors="replace")[:8000]
            return ("passed" if rc == 0 else "failed"), output, rc
        except Exception as e:
            return "failed", str(e), -1
        finally:
            if os.path.exists(script_path):
                try: os.remove(script_path)
                except OSError: pass
