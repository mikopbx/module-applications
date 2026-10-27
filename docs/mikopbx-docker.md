# MikoPBX in Docker: test environment and the module lifecycle by hand

This page records what the checks rely on. All of it was observed on x86_64 (a cloud container, Docker 29.9)
on 2026-10-27. arm64 has not been run yet (request to the Mac session).

## Image

`mikopbx/mikopbx` on Docker Hub. Tags and digests read from the Docker Hub API on 2026-10-27:

| Tag | Digest | Architectures |
|---|---|---|
| `2026.6.34` = `latest` | `sha256:ff5cc0238a4e170bfbf8c0be9a59b58c8affb6fb24b36d1506f5fc0cb41f7691` (multi-arch index) | amd64, arm64 |
| `latest-amd64` | `sha256:50ed89821560291b7928e92390f7a844e065c9138c5e7221c63370cf41fe1f5d` | amd64 |
| `latest-arm64` | `sha256:3af27a8ecc070108bdde4a2e3939407eeda0574896e55a547ee221973d0b6a6c` | arm64 |
| `2026.6.43-dev` = `dev` | `sha256:ed0ba54e2f9a6fad4a7b98249893f5f839b533d1845c769e31a8f9b4e6d1fa44` (multi-arch index) | amd64, arm64 |

The checks pull the multi-arch index by digest (`mikopbx/mikopbx@sha256:ff5c…`), so each runner gets its own
architecture and the digest cannot move under us. The digest comes from `config/mikopbx.yaml`, never from an
application.

An anonymous pull once returned `429 Too Many Requests` (Docker Hub rate limit). A second attempt worked.

## Running it

```sh
docker network create --internal mpbx-isolated
docker run -d --name mpbx --hostname mikopbx --network mpbx-isolated \
  --cpus 2 --memory 3g --pids-limit 2048 \
  mikopbx/mikopbx@sha256:ff5cc0238a4e170bfbf8c0be9a59b58c8affb6fb24b36d1506f5fc0cb41f7691
```

- `--privileged` is **not** needed. No volumes are needed: the data lives in the container layer, which is what a
  throw-away test wants. No ports are published.
- **Network.** Without a network (`--network none`) the system does not come up: there is no ethernet interface,
  "Waiting for Asterisk to fully boot" fails after 60 s, and the REST API answers only after ~140 s.
  On a Docker `--internal` network it starts as usual and nothing outside is reachable
  (`curl https://releases.mikopbx.com` and `curl http://169.254.169.254/` from inside both fail).
  With an ordinary network the image itself calls out (for example `releases.mikopbx.com` for `getModuleInfo`).
- **Start-up.** The REST API answers about 18 s after `docker run` (poll `GET /pbxcore/api/v3/system:ping`).
- **Authentication.** Every call is made from inside the container to `http://127.0.0.1`. The modules resource is
  `ResourceSecurity('modules', [LOCALHOST, BEARER_TOKEN])` (Core `src/PBXCoreREST/Controllers/Modules/RestController.php`),
  so loopback needs no password and no credentials exist anywhere in the checks. The web credentials printed in the
  log (`admin`/`admin`) are not used.

## The lifecycle, call by call

`A=http://127.0.0.1/pbxcore/api/v3`, all run with `docker exec mpbx curl ...`.

1. **Package.** The ZIP holds the repository's files at the archive root (`module.json` at the root; a wrapping folder
   fails with `ext_ModuleJsonNotFound`). ModuleTemplate's `module.json` has `"version": "%ModuleVersion%"`, which MIKO's
   release pipeline replaces; the checks replace it too (see `tools/check`).
2. **Copy in.** `docker cp Module.zip mpbx:/tmp/module.zip`
3. **Upload** (resumable.js protocol, one chunk): `POST $A/files:upload` as multipart with `resumableIdentifier`,
   `resumableFilename`, `resumableTotalSize`, `resumableChunkNumber=1`, `resumableTotalChunks=1`,
   `resumableChunkSize`, `resumableCurrentChunkSize`, `file_mime_type=application/zip`, `file=@/tmp/module.zip`.
   The answer's `data.filename` is the path to use below. Then poll
   `GET $A/files:uploadStatus?resumableIdentifier=<id>` until `d_status=UPLOAD_COMPLETE`.
4. **Read the id.** `POST $A/modules:getMetadataFromPackage {"filePath": "<filename>"}` returns `data.uniqid`.
5. **Install.** `POST $A/modules:installFromPackage {"filePath": "<filename>", "fileId": "<id>"}` returns
   `data.operationId`. The operation runs in the background.
6. **Wait.** `GET $A/modules/<module_id>:getOperationStatus?operationId=<op>` (or `GET $A/modules:getOperations`)
   until `terminal=true`. Success is `state=completed` with empty `errorMessages`.
   `modules:getInstallationStatus` does not work with this pipeline (`PROGRESS_FILE_NOT_FOUND`).
7. **Installed, disabled.** The row appears in `m_PbxExtensionModules` with `disabled=1`, and the files under
   `/storage/usbdisk1/mikopbx/custom_modules/<module_id>/`.
8. **Enable.** `POST $A/modules/<module_id>:enable {}` → `disabled=0`. ModuleTemplate's two workers
   (`Modules\ModuleTemplate\Lib\WorkerTemplateMain`, `...WorkerTemplateAMI`) appear in `ps`, and its own API answers:
   `GET http://127.0.0.1/pbxcore/api/modules/ModuleTemplate/check` → `result=true`.
9. **Disable.** `POST $A/modules/<module_id>:disable {}` → `disabled=1`, the workers are gone.
10. **Uninstall.** `POST $A/modules/<module_id>:uninstall {"keepSettings": false}` → the row and the directory are gone.
    In 2026.6.34 the body **must** contain `keepSettings`: `{}` fails with
    `ErrorException: Undefined array key "keepSettings"` (fixed on Core develop, #1265).
11. **Still alive.** `GET $A/system:ping` → `result=true`; `asterisk -rx 'core show uptime'` answers.

Two more differences from Core develop in 2026.6.34:

- `GET $A/modules` (getList) answers `Unknown action - getList`, so the checks read the installed modules from
  `/cf/conf/mikopbx.db` (`sqlite3 ... "select uniqid, version, disabled from m_PbxExtensionModules"`).
- Logs: `/storage/usbdisk1/mikopbx/log/{system/messages, php/, asterisk/, nginx/}`; a module gets its own
  `log/<module_id>/`. Module operations are logged to `system/messages` (`Module operation started: {...}`).
