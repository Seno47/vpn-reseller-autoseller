from __future__ import annotations

import ast
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class FrontendDeploymentContractTests(unittest.TestCase):
    def read(self, relative_path: str) -> str:
        return (ROOT / relative_path).read_text(encoding="utf-8")

    def test_docker_image_listens_on_the_container_interface(self) -> None:
        dockerfile = self.read("Dockerfile")

        self.assertIn("APP_HOST=0.0.0.0", dockerfile)
        self.assertIn("chown appuser:appuser /app/data", dockerfile)
        self.assertNotIn("chown -R appuser:appuser /app", dockerfile)

    def test_docker_context_excludes_secrets_and_runtime_artifacts(self) -> None:
        ignored = set(self.read(".dockerignore").splitlines())

        self.assertTrue(
            {".env", ".git", ".venv", "data", "output", "*.log", "**/__pycache__", ".pytest_cache"}
            <= ignored
        )

    def test_reinstall_preserves_runtime_data_and_code_is_root_owned(self) -> None:
        installer = self.read("scripts/install-linux.sh")
        local_copy = installer[installer.index("sync_source_tree()") : installer.index("secure_app_permissions()")]

        self.assertIn('--exclude "data"', local_copy)
        for pattern in (".git", ".env", ".venv", ".pytest_cache", "__pycache__", "backups", "data", "output", "*.log"):
            self.assertIn(f'--exclude "{pattern}"', local_copy)
        self.assertNotIn('rm -rf "$APP_DIR"', installer)
        self.assertIn('sync_source_tree "$temporary_dir/source"', installer)
        self.assertNotIn('git -C "$APP_DIR" pull', installer)
        self.assertNotIn('rm -f -- "$APP_DIR/.env"\n', installer)
        self.assertIn('venv_new="$(mktemp -d "$APP_DIR/.venv-new.XXXXXX")"', installer)
        self.assertIn('env_new="$(mktemp "$APP_DIR/.env-new.XXXXXX")"', installer)
        self.assertIn('mv -T -- "$venv_new" "$APP_DIR/.venv"', installer)
        self.assertIn('mv -T -- "$env_new" "$APP_DIR/.env"', installer)
        self.assertIn("trap '' INT TERM", installer)
        self.assertIn('for child in data .env .venv backups .git', installer)
        self.assertIn('claim_install_target_boundary', installer)
        self.assertIn('if [ "$source_real" = "$app_real" ]', installer)
        self.assertNotIn('chown -R "$APP_USER:$APP_USER" "$APP_DIR"', installer)
        self.assertIn('chown "root:$APP_USER" "$APP_DIR/.env"', installer)
        self.assertIn("APP_UPDATE_STATUS_FILE=/run/${APP_NAME}/update-status.json", installer)
        self.assertIn("RuntimeDirectory=${APP_NAME}", installer)
        self.assertIn("Environment=APP_UPDATER_PATH=${updater}", installer)
        self.assertIn("Environment=APP_PYTHON_BIN=${PYTHON_BIN}", installer)
        self.assertIn("Type=oneshot\nUser=root\nGroup=root", installer)
        self.assertIn("Type=oneshot\nUser=root\nGroup=root\nUMask=0022", installer)
        self.assertIn('chmod 0700 "$APP_DIR/backups"', installer)
        self.assertIn("chmod u+rwX,go+rX,go-w", installer)
        self.assertIn('app_real="$(realpath -m -- "$APP_DIR")"', installer)
        self.assertIn('/|/bin|/bin/*|/boot|/boot/*', installer)
        self.assertIn('/etc|/etc/*', installer)
        self.assertIn("parent_uid=\"$(stat -c '%u' -- \"$existing_parent\")\"", installer)
        self.assertIn('${parent_mode:5:1}', installer)
        self.assertIn('${parent_mode:8:1}', installer)
        self.assertIn("Application parent must be root-owned", installer)
        self.assertIn('[ -f "$APP_DIR/run.py" ] && [ ! -L "$APP_DIR/run.py" ]', installer)
        self.assertIn('[ -f "$APP_DIR/requirements.txt" ] && [ ! -L "$APP_DIR/requirements.txt" ]', installer)
        self.assertIn('find "$APP_DIR" -xdev', installer)
        self.assertIn("need_root\nvalidate_install_target", installer)
        self.assertIn('Refusing to replace a non-empty directory without application markers', installer)
        self.assertIn('find "$APP_DIR" -mindepth 1 -maxdepth 1 -print -quit', installer)
        self.assertLess(local_copy.index("claim_install_target_boundary"), local_copy.index('rsync -a --delete'))
        self.assertLess(local_copy.index("reject_unsafe_preserved_paths"), local_copy.index('rsync -a --delete'))
        self.assertIn("dotenv_quote()", installer)
        self.assertIn('ADMIN_PASSWORD=${admin_password_value}', installer)
        self.assertNotIn('ADMIN_PASSWORD=${ADMIN_PASSWORD}', installer)

    def test_update_request_cannot_override_root_owned_source(self) -> None:
        updater = self.read("scripts/update-linux.sh")
        source_sync = updater[updater.index("install_from_git()") : updater.index("refresh_host_updater()")]

        self.assertNotIn("read_request_value", updater)
        self.assertNotIn("REQUEST_REPO", updater)
        self.assertNotIn("REQUEST_BRANCH", updater)
        self.assertLess(updater.index('REQUEST_ID="$(consume_request)"'), updater.index("git clone"))
        self.assertIn('git clone --depth 1 --branch "$APP_BRANCH" -- "$APP_REPO_URL"', updater)
        self.assertIn('--exclude "backups"', source_sync)
        self.assertNotIn("umask 077", updater)
        self.assertIn('chmod 0700 "$APP_DIR/backups"', updater)
        self.assertIn('find "$APP_DIR/backups" -xdev -type f -exec chmod 0600 {} +', updater)
        self.assertIn("chmod u+rwX,go+rX,go-w", updater)
        self.assertIn('app_real="$(realpath -m -- "$APP_DIR")"', updater)
        self.assertIn('/|/bin|/bin/*|/boot|/boot/*', updater)
        self.assertIn("Application parent must be root-owned", updater)
        self.assertIn('[ -f "$APP_DIR/app/run.py" ]', updater)
        self.assertIn('find "$APP_DIR" -xdev', updater)
        self.assertIn('for child in data .env .venv backups .git app/.env app/.venv app/.git', updater)
        self.assertIn('VENV_NEW="$(mktemp -d "$APP_DIR/.venv-new.XXXXXX")"', updater)
        self.assertNotIn('"$APP_DIR/.venv/bin/python" -m pip', updater)
        self.assertIn('BACKUP_TMP="$(mktemp "$APP_DIR/backups/.source-before-update.XXXXXX.tar.gz")"', updater)
        self.assertIn('mv -Tf -- "$BACKUP_TMP"', updater)
        backup_command = updater[updater.index('BACKUP_TMP="$(mktemp "$APP_DIR/backups/.source-before-update.') :]
        for pattern in (".git", ".env", "data", "backups", "output", "*.log"):
            self.assertIn(f'--exclude "{pattern}"', backup_command)

    def test_updater_consumes_requests_and_uses_safe_atomic_status_writes(self) -> None:
        updater = self.read("scripts/update-linux.sh")

        self.assertIn('getattr(os, "O_NOFOLLOW", 0)', updater)
        self.assertIn("os.O_NONBLOCK", updater)
        self.assertIn("allowed_uids.add(parent_metadata.st_uid)", updater)
        self.assertIn("os.unlink(path)", updater)
        self.assertIn("tempfile.mkstemp", updater)
        self.assertIn("os.replace(temporary_path, path)", updater)
        self.assertIn("encoded_value =", updater)
        self.assertIn("trap 'handle_error", updater)
        self.assertIn('write_status "error" "request"', updater)
        self.assertNotIn('chown -R "$APP_USER:$APP_USER" "$APP_DIR"', updater)
        self.assertIn('refresh_host_updater "$APP_DIR/scripts/update-linux.sh"', updater)
        self.assertIn("os.rename(path, rejected_path)", updater)

    def test_updater_writes_docker_env_values_without_literal_quotes(self) -> None:
        updater = self.read("scripts/update-linux.sh")
        function = updater[updater.index("upsert_env()") : updater.index("validate_app_dir_path()")]
        block = re.search(r"<<'PY'\n(.*?)\nPY", function, flags=re.DOTALL)
        self.assertIsNotNone(block)

        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text('APP_UPDATE_CURRENT_COMMIT="old"\n', encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    "-",
                    str(env_file),
                    "APP_UPDATE_CURRENT_COMMIT",
                    "1e6d794f6c75",
                ],
                input=(
                    "import os\n"
                    "if not hasattr(os, 'fchmod'): os.fchmod = lambda *_: None\n"
                    "if not hasattr(os, 'fchown'): os.fchown = lambda *_: None\n"
                    + block.group(1)
                ),
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                env_file.read_text(encoding="utf-8"),
                "APP_UPDATE_CURRENT_COMMIT=1e6d794f6c75\n",
            )

    def test_update_documentation_explains_the_privilege_boundary(self) -> None:
        readme = self.read("README.md")

        self.assertIn("содержимое request-файла не может их переопределить", readme)
        self.assertIn("Код, `.venv` и бэкапы принадлежат `root`", readme)
        self.assertIn("один раз повторно запустите актуальный", readme)

    def test_embedded_deployment_python_is_valid(self) -> None:
        for relative_path in ("scripts/install-linux.sh", "scripts/update-linux.sh"):
            script = self.read(relative_path)
            blocks = re.findall(r"<<'PY'\n(.*?)\nPY", script, flags=re.DOTALL)
            self.assertTrue(blocks, relative_path)
            for index, block in enumerate(blocks):
                ast.parse(block, filename=f"{relative_path}:heredoc-{index}")

    def test_external_order_ids_are_escaped_before_entering_attributes(self) -> None:
        javascript = self.read("reseller_autoseller/static/app.js")

        unsafe_attributes = re.findall(
            r'data-(?:event-sale|pending-events)="\$\{row\.marketplace\}:\$\{row\.external_order_id\}"',
            javascript,
        )
        escaped_attributes = re.findall(
            r'data-(?:event-sale|pending-events)="\$\{escapeHtml\(`\$\{row\.marketplace\}:\$\{row\.external_order_id\}`\)\}"',
            javascript,
        )
        self.assertEqual(unsafe_attributes, [])
        self.assertEqual(len(escaped_attributes), 2)

    def test_numeric_settings_and_save_errors_have_browser_feedback(self) -> None:
        javascript = self.read("reseller_autoseller/static/app.js")

        self.assertIn('row.kind === "number" ? "number"', javascript)
        self.assertIn("row.kind === \"number\" ? ' step=\"any\"'", javascript)
        self.assertIn('t("Не удалось сохранить настройки", "Could not save settings")', javascript)
        self.assertIn('typeof payload?.detail === "string"', javascript)

    def test_update_controls_are_visible_once_at_the_top_of_the_panel(self) -> None:
        html = self.read("reseller_autoseller/static/index.html")
        javascript = self.read("reseller_autoseller/static/app.js")
        stylesheet = self.read("reseller_autoseller/static/styles.css")
        topbar = html[html.index('<section class="topbar">') : html.index('<section class="metrics"')]
        diagnostics = html[html.index('data-section="diagnostics"') :]

        for element_id in ("updateStatus", "updateNotice", "checkUpdateButton", "startUpdateButton"):
            self.assertEqual(html.count(f'id="{element_id}"'), 1)
            self.assertIn(f'id="{element_id}"', topbar)
            self.assertNotIn(f'id="{element_id}"', diagnostics)

        self.assertIn('id="refreshButton" class="secondary" type="button">Обновить статус</button>', topbar)
        self.assertIn('id="checkUpdateButton" type="button">Проверить обновления</button>', topbar)
        self.assertIn('id="startUpdateButton" type="button" disabled>Обновить версию</button>', topbar)
        self.assertIn('role="status" aria-live="polite" aria-atomic="true"', topbar)
        self.assertIn('id="updateStatus" aria-describedby="updateNotice" aria-busy="true"', topbar)
        self.assertIn('"Обновить статус": "Refresh status"', javascript)
        self.assertIn('"Проверить обновления": "Check for updates"', javascript)
        self.assertEqual(javascript.count('api("/admin/api/update/check"'), 1)
        self.assertEqual(javascript.count('api("/admin/api/update/start"'), 1)
        self.assertIn('button:focus-visible', stylesheet)
        self.assertIn('button[aria-busy="true"]', stylesheet)

        css_version = re.search(r'/static/styles\.css\?v=([^"\s]+)', html)
        js_version = re.search(r'/static/app\.js\?v=([^"\s]+)', html)
        self.assertIsNotNone(css_version)
        self.assertIsNotNone(js_version)
        self.assertEqual(css_version.group(1), js_version.group(1))

    def test_admin_shell_keeps_accessible_responsive_ui_contracts(self) -> None:
        html = self.read("reseller_autoseller/static/index.html")
        javascript = self.read("reseller_autoseller/static/app.js")
        stylesheet = self.read("reseller_autoseller/static/styles.css")

        self.assertIn('class="skip-link" href="#adminContent"', html)
        self.assertIn('id="adminContent" tabindex="-1"', html)
        self.assertIn('class="section-tabs" aria-label="Разделы панели" role="tablist"', html)
        self.assertEqual(html.count('role="tab"'), 7)
        self.assertEqual(html.count('role="tabpanel"'), 7)
        self.assertIn('button.setAttribute("aria-selected", String(active))', javascript)
        self.assertIn('event.key === "ArrowRight"', javascript)
        self.assertIn('event.key === "ArrowLeft"', javascript)
        self.assertIn('prefers-reduced-motion: reduce', javascript)

        for token in (
            "--color-background:",
            "--color-surface:",
            "--color-text:",
            "--color-brand:",
            "--shadow-card:",
            "--radius-lg:",
            "--motion-fast:",
        ):
            self.assertIn(token, stylesheet)
        self.assertIn("min-height: 44px", stylesheet)
        self.assertIn("@media (max-width: 1100px)", stylesheet)
        self.assertIn("@media (max-width: 768px)", stylesheet)
        self.assertIn("@media (max-width: 560px)", stylesheet)
        self.assertIn("@media (prefers-reduced-motion: reduce)", stylesheet)
        self.assertRegex(stylesheet, r"\.panel\s*\{[^}]*width:\s*100%;[^}]*min-width:\s*0;")
        self.assertIn(".product-grid .mapping-source-actions", stylesheet)


if __name__ == "__main__":
    unittest.main()
