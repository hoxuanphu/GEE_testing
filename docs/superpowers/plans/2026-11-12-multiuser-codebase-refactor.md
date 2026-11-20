# Multiuser Codebase Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Chỉ triển khai sau khi người dùng yêu cầu thực hiện; không tự tạo branch/worktree hoặc push.

**Goal:** Chuẩn hóa GEE_testing để nhiều người dùng chạy tác vụ bằng tài khoản GEE/CDSE riêng, đồng thời giữ luồng local và tính đúng đắn của raster hiện có.

**Architecture:** FastAPI theo module; PostgreSQL lưu owner/snapshot/job/quota/lease;
worker độc lập tạo process riêng cho từng tác vụ GEE. Giữ kernel raster và UI
Leaflet hiện tại, đưa server workflow nặng qua API v2 bất đồng bộ.

**Tech Stack:** Python 3.12/FastAPI/Pydantic/Rasterio/GDAL/NumPy/requests/EE SDK,
JavaScript/Leaflet/node:test hiện có; PostgreSQL 16 và container cho server đề xuất.
Dependency server cần review/lock riêng: SQLAlchemy 2, Alembic, psycopg 3,
Authlib và cryptography; chưa cài hoặc chọn version chính xác trong lượt lập kế hoạch.

**Spec:** [Thiết kế nhiều người dùng](../specs/2026-11-12-multiuser-refactor-design.md).

**Trạng thái:** Draft for review; R00–R13 đều chưa thực hiện.

## Global Constraints

- Mỗi người kết nối tài khoản GEE/CDSE riêng; không fallback sang tài khoản máy chủ.
- Profile `local` giữ hợp đồng API và xử lý ảnh; profile `server` yêu cầu auth,
  owner và worker, fail startup khi thiếu cấu hình bắt buộc.
- Tác vụ GEE chạy process riêng; không gọi `ee.Initialize` đổi tài khoản trong
  request/thread dùng chung, kể cả search/preview/preflight.
- Giữ ROI/CRS/affine/shape/band/scale/mask/NoData/provenance và giới hạn từng workflow.
- S2 `None` = 26 band, `[]` = 12 band phổ; matched-frame giữ preset riêng.
- EODAG optional/default-off/catalogue-only; không tự bật download hoặc fallback.
- Không sửa snapshot/digest lịch sử, không đọc secret thật để ghi vào tài liệu,
  không đưa key/token/password/signed URL vào DB payload, logs hoặc browser storage.
- Không đổi branch/worktree, reset/clean, gom dirty tree, commit hoặc push ngoài
  phạm vi được phép. Mỗi task hoàn tất tạo diff reviewable và checkpoint; commit
  chỉ chọn file/hunk của task sau khi được phép.
- PostgreSQL/2 API/2 worker và live provider có acceptance riêng; SQLite/mock/unit
  không được dùng để công bố concurrency, BYOC hoặc production readiness.

## Review Focus

1. A gửi ID của B qua preview, pagination, preflight, cancel, download hoặc restore:
   trả 404, không chạm TTL/quota của B — R03/R04/R06/R09.
2. Search/preview vô tình dùng EE hoặc CDSE cache của user trước: tất cả provider
   operation có identity tường minh và process isolation — R02/R05/R08.
3. Worker chết khi file đã rename nhưng DB chưa commit; attempt cũ quay lại sau
   lease expiry: không publish hai kết quả hoặc giải phóng quota hai lần — R07/R08.
4. Người dùng logout/revoke trong lúc poll/tải; cleanup chạy ở process khác:
   response cũ không khôi phục UI, artifact đang stream không bị xóa — R06/R10/R12.
5. Hai API replica cùng startup recovery hoặc cùng nhận POST retry: không ngắt
   worker còn lease, không chạy job trùng; snapshot local thiếu owner không public — R01/R07/R11.

## 1. Khảo sát và baseline

Source snapshot 2026-11-12: branch `feature/eodag-catalog-mvp`, HEAD `7e6ec12`;
working tree đã dirty trước kế hoạch. Phần lớn tính năng đồng khung và một số UI/test
chưa tracked. Không dùng diff so với HEAD như danh sách toàn bộ thay đổi refactor.

| Điểm cần xử lý | File hiện tại | Hướng |
|---|---|---|
| Bootstrap/global services + 56 API operation | `app/main.py` (2.788 dòng) | App factory, explicit services, APIRouter theo workflow |
| EE xử lý ảnh lẫn URL/HTTP/merge | `app/imagery.py` (1.729 dòng) | Tách module, facade tương thích |
| Credential dùng chung | `app/ee_session.py`, `app/cdse.py` (991 dòng) | Per-user vault, transport theo context, process isolation |
| Import ngược bootstrap | `app/scene_catalog.py`, `app/s5p.py` | Service injection; model/router độc lập |
| Local job stores/locks/recovery | `app/timeseries.py`, `app/frame_store.py`, `app/frame_jobs.py`, `app/esri.py` | PostgreSQL adapters và worker server, giữ local adapters |
| Inline UI và job restore global | `static/index.html` (2.137 dòng), `static/matched-frame.js` | Feature controllers, auth-aware API, user-scoped state |
| Scaffold phân tích chưa đầy đủ | `frontend/`, `app/analysis_models.py` | Giữ ngoài đường găng, không gộp NDVI vào delivery |

Lệnh baseline chuẩn của repo:

```powershell
.venv\Scripts\python.exe -m unittest discover tests -v
node --test tests/*.cjs
```

Kết quả chạy lại trong lượt lập kế hoạch: **sẽ ghi sau khi lệnh hoàn tất**.
Không chạy `npm build` cho scaffold NDVI để suy ra trạng thái UI đang phục vụ.

## 2. Cấu trúc đích

Các đường dẫn sau là đề xuất, chưa tồn tại trừ khi ghi là giữ lại:

```text
app/
  main.py                       # facade/entrypoint local tương thích
  bootstrap.py                  # create_app, middleware, routes, lifespan
  config.py                     # Settings và profile local/server
  context.py                    # Principal, RequestContext
  api/
    dependencies.py             # principal/service injection
    models/{optical,sar,timeseries,catalog,s5p,operations}.py
    routes/{geo_admin,optical,dem,sar_gee,sar_cdse,products_cdse}.py
    routes/{optical_cdse,optical_aws,catalog,timeseries,esri,s5p,matched_frame}.py
    routes/{auth,connections,operations,artifacts,health}.py
  services/{selection,optical_exports,cdse_exports,aws_exports}.py
  auth/{oidc,sessions,csrf,policy}.py
  credentials/{models,vault,gee,cdse}.py
  providers/gee/{bands,collections,selection,composites,preview,export}.py
  providers/cdse/{catalog,odata,s3}.py
  persistence/{database,tables,repositories,local_adapters}.py
  jobs/{models,repository,admission,scheduler,worker,handlers}.py
  artifacts/{store,leases,migration}.py
  observability/{logging,metrics,audit}.py
  worker.py                     # python -m app.worker
  imagery.py, cdse.py            # facade cũ trong giai đoạn tương thích
  geo.py, raster_grid.py         # giữ kernel, không sửa thuật toán trong lần tách
  frame_*.py, reference_frame.py # giữ kernel/contracts, đổi chỗ orchestration từng bước
static/
  index.html
  app/{api-client,auth,bootstrap,optical,catalog,timeseries,sar,downloads}.js
  matched-frame.js, rectangle.js, selection-state.js, s2-layers.js
migrations/                     # Alembic, server DB có schema version
scripts/{capture_baseline,migrate_local_data,validate_refactor_plan}.py
tests/{contracts,integration,server,browser}/
deploy/{compose.yaml,proxy.conf,Dockerfile.api,Dockerfile.worker}
docs/evidence/multiuser-refactor/
```

Chỉ tạo package/file khi task tương ứng cần; không scaffold toàn cây trước.
Không chuyển `aws_stac.py`, `esri.py`, `dem.py` thành framework provider chung nếu
chưa có điểm dùng; không đồng nhất semantics các provider chỉ để giảm số dòng.

## 3. Thứ tự và điều kiện bàn giao

| Giai đoạn | Task | Bàn giao độc lập |
|---|---|---|
| A — Chốt hợp đồng, tách mã | R00–R01 | Local vẫn chạy, import không tạo worker/DB side effect |
| B — Định danh và tài khoản riêng | R02–R05 | Auth/provider capability đã chứng minh, owner/vault được kiểm thử |
| C — Artifact, quota, worker | R06–R08 | Hai user/replica không xung đột hoặc dùng lẫn credentials |
| D — Tích hợp workflow và UI | R09–R10 | UI server dùng v2; local vẫn tương thích |
| E — Migration và vận hành | R11–R12 | Có rehearsal/rollback/monitoring, chưa mở production |
| F — Nghiệm thu | R13 | Chỉ bật server sau khi mọi gate có bằng chứng |

R01 và R02 có thể khảo sát độc lập; R03 sau R01; R04 sau R03; R05 sau
R02/R04; R06 sau R03/R04; R07 sau R03/R04; R08 sau R05/R06/R07;
R09 sau R08; R10 sau R09; R11 sau R06/R09; R12 sau R08/R10/R11;
R13 sau tất cả. Các mũi tên này không phải cho phép tự gọi agent hoặc triển khai.

### R00 — Khóa baseline và bản đồ hợp đồng

**Files:** Create `scripts/capture_baseline.py`, `tests/contracts/test_local_api_contract.py`,
`docs/evidence/multiuser-refactor/baseline/{inventory.json,openapi.json,contracts.json}`;
read `README.md`, `plan/`, `docs/superpowers/`, các test hiện có.

**Interfaces:** `capture_baseline(root: Path, output: Path) -> dict` sinh manifest
đường dẫn/sha256 file nguồn được allowlist, routes qua OpenAPI, model schema và
fixture expectations. Không đọc `.env`, key/PEM, downloads hay binary vào manifest.

- [ ] Kiểm tra lại branch/dirty files, ghi inventory có source hash để phân biệt
  thay đổi cũ với task; xác nhận phạm vi snapshot trước khi tạo commit baseline.
- [ ] Characterization test giữ 56 operation hiện tại, error/header/filename,
  end-date, ROI/platform, S2 `None`/`[]`/subset, frame preset/digest và EODAG disabled.
  Test này có thể GREEN ngay; không bịa TDD RED cho hành vi đã có.
- [ ] Chạy hai lệnh baseline ở mục 1; lưu count, skip reason, environment/package
  versions và source hash. Thất bại phải ghi tên test, không sửa expected để qua.
- [ ] Tạo checkpoint R00 với danh sách hợp đồng và giới hạn kiểm chứng; fixture
  không chứa signed URL, private ROI/user identity thật hoặc secret.

### R01 — Tách bootstrap, API và kernel nhưng giữ local contract

**Files:** Modify `app/main.py`, `app/imagery.py`, `app/cdse.py`, `app/scene_catalog.py`,
`app/s5p.py`; create `app/bootstrap.py`, `app/config.py`, `app/api/dependencies.py`,
model/router/service/provider modules trong mục 2; update các test patch vào `main`.

**Interfaces:** `create_app(settings: Settings, services: AppServices | None = None) -> FastAPI`;
`build_local_services(settings: Settings) -> AppServices`;
`get_services(request: Request) -> AppServices`. `AppServices` định nghĩa ở
`app/api/dependencies.py`; fields `selection`, `optical`, `cdse`, `aws`,
`timeseries`, `frames`, `esri`, `catalog`, `s5p` có protocol theo chữ ký cũ tương ứng.
`Settings` ở `app/config.py` có `mode: Literal['local','server']`, storage/database/
OIDC/origin settings; parse config một lần, không đọc `.env` trong request.

- [ ] Thêm `tests/test_app_factory.py`: hai app có service/store khác nhau không
  chia sẻ job; import bootstrap không tạo DB/file/worker hoặc gọi provider;
  model/router/provider không import `app.main`; server config thiếu bị từ chối.
- [ ] Tách theo thứ tự: request/selection → route geo/DEM → optical/SAR → CDSE/AWS
  → catalog/S5P → job routes. Mỗi lát chạy test cũ của lát đó trước lát tiếp theo.
  Legacy facades re-export hàm công khai; test dùng factory/service override,
  không duy trì monkey-patch ngược `main` làm dependency dài hạn.
- [ ] Tách `imagery` và `cdse` theo package ở mục 2 bằng di chuyển logic, giữ facade;
  chưa thay thuật toán mask/composite/warp, grant, retry hoặc giá trị mặc định.
- [ ] Chạy `.venv\Scripts\python.exe -m unittest tests.test_app_factory tests.contracts.test_local_api_contract tests.test_api tests.test_imagery tests.test_cdse tests.test_scene_catalog tests.test_s5p tests.test_s2_auxiliary -v`,
  sau đó toàn bộ Python + JS. So baseline OpenAPI/fixture, review diff và checkpoint R01.

### R02 — Chứng minh đường kết nối tài khoản cá nhân

**Files:** Create `docs/evidence/multiuser-refactor/provider-auth-matrix.md`,
`app/credentials/models.py`, `tests/test_provider_capabilities.py`,
`scripts/validate_personal_provider_auth.py`; read `app/ee_session.py`, `app/cdse.py`,
`app/frame_search.py`, `.env.example` và tài liệu chính thức của provider.

**Interfaces:** `ProviderConnectionSpec(provider, grant, subject, project_id, scopes)`
và `ConnectionCapability(provider, operation, supported, reason)` là dataclass frozen;
`validate_capability(spec: ProviderConnectionSpec, operation: str) -> ConnectionCapability`.
Không đưa secret vào dataclass có thể serialize/log.

- [ ] Lập matrix GEE search/preview/export, CDSE public search/OData/S3/full ZIP/bands,
  các scope/project/client/grant thật sự dùng được; ghi URL docs/ngày kiểm tra.
- [ ] Test từ chối Sentinel Hub token cho OData, không tự tạo S3 key, thiếu EE project,
  không có refresh grant, không dùng credential máy chủ khi user chưa kết nối.
- [ ] Smoke tối thiểu hai tài khoản được phép ở môi trường riêng; chỉ ghi identity
  giả danh + kết quả capability. Không ghi token hoặc yêu cầu người dùng đưa secret
  vào chat. Chưa có tài khoản/client hợp lệ: đánh dấu gate tương ứng BLOCKED,
  tiếp tục phần không phụ thuộc, không giả thành supported.
- [ ] Chốt thư viện/version auth/crypto/DB qua kiểm tra tương thích và file
  `requirements-server.in`, `requirements-server.lock.txt` sinh bằng công cụ khóa
  dependency được ghi lại; giữ `requirements.txt` và pin EODAG hiện có.
  Chạy `.venv\Scripts\python.exe -m unittest tests.test_provider_capabilities -v`;
  checkpoint capability/evidence, không bật provider flow chưa chứng minh.

### R03 — Context, PostgreSQL và repository có owner

**Files:** Create `app/context.py`, `app/persistence/{database,tables,repositories}.py`,
`migrations/`, `alembic.ini`, `tests/server/test_owner_repositories.py`,
`tests/server/test_database_migrations.py`; add test DB configuration.

**Interfaces:** frozen `Principal(user_id: UUID, issuer: str, subject: str, roles: frozenset[str])`,
`RequestContext(principal: Principal, request_id: str)`;
`SnapshotRepository.create(ctx, kind: str, payload: dict) -> Snapshot`;
`SnapshotRepository.get(ctx, snapshot_id: UUID) -> Snapshot`;
`JobRepository.get(ctx, job_id: UUID) -> JobRecord`. `Snapshot`/`JobRecord` nằm ở
`app/persistence/tables.py`; immutable payload, explicit schema/version/hash.

- [ ] RED: A/B truy cập search/reference/preflight/job/credential của nhau trả NotFound;
  cross-owner foreign key bị DB từ chối; unauthorized lookup không đổi activity.
- [ ] Migration tạo users `(issuer,subject) UNIQUE`, sessions, provider_connections,
  snapshots, jobs, attempts, artifacts, download_leases, reservations, audit_events;
  owner NOT NULL và composite owner/id FK; job/schema versions tách khỏi payload digest.
- [ ] DB transaction/session request-scoped; API app không tự tạo/nâng schema startup.
  Tạo fixture PostgreSQL thật, không thay integration DB bằng SQLite.
- [ ] Chạy `.venv\Scripts\python.exe -m unittest tests.server.test_owner_repositories tests.server.test_database_migrations -v`
  với `TEST_DATABASE_URL`; thiếu DB phải báo thiếu evidence. Test upgrade/rollback
  trên DB throwaway, kiểm tra dữ liệu/hash rồi checkpoint R03.

### R04 — OIDC/session và bảo vệ toàn bộ API server

**Files:** Create `app/auth/{oidc,sessions,csrf,policy}.py`, `app/api/routes/auth.py`,
`tests/server/test_auth_sessions.py`, `tests/server/test_route_policy.py`;
modify `app/api/dependencies.py`, `app/bootstrap.py`, `app/config.py`.

**Interfaces:** `require_principal(request: Request) -> Principal`;
`SessionService.create(identity: VerifiedIdentity) -> SessionHandle`;
`SessionService.resolve(cookie: str) -> Principal`; `SessionService.revoke(session_id: UUID) -> None`.
Các type session ở `app/auth/sessions.py`, VerifiedIdentity ở `app/auth/oidc.py`;
không nhận role/user từ header tự khai báo.

- [ ] RED: issuer/audience/nonce/state/expiry sai, code replay, session fixation,
  CSRF/cross-origin mutation, logout/revoked session, proxy giả đều bị từ chối.
- [ ] Thêm `/auth/login`, `/auth/callback`, `/auth/logout`, `/api/me`; server-side
  opaque session, cookie flags và timeout theo spec; không tự viết bộ verify JWT.
- [ ] Default-deny server API: allowlist health/static/auth; data routes yêu cầu
  principal; v1 xử lý nặng chưa chuyển đổi trả `409 legacy_job_required`.
  Kiểm kê OpenAPI cùng route/mount động; router mới không được lọt qua policy.
- [ ] Chạy `.venv\Scripts\python.exe -m unittest tests.server.test_auth_sessions tests.server.test_route_policy tests.contracts.test_local_api_contract -v`;
  local behavior phải giữ; checkpoint R04 kèm các API server bị chặn tạm thời.

### R05 — Vault và kết nối GEE/CDSE theo user

**Files:** Create `app/credentials/{vault,gee,cdse}.py`, `app/api/routes/connections.py`,
`tests/server/test_credential_vault.py`, `tests/server/test_personal_connections.py`;
modify provider clients ở R01, `app/ee_session.py`, `app/config.py`.

**Interfaces:** `CredentialVault.store(ctx, provider: str, secret: SecretMaterial) -> CredentialRef`;
`CredentialVault.resolve_for_job(lease: JobLease) -> ResolvedCredential`;
`CredentialVault.revoke(ctx, credential_id: UUID) -> None`;
`CredentialRef(id: UUID, version: int)`; types secret không repr/serialize được.
`JobLease` do R07 định nghĩa; adapter resolve chỉ thực thi sau R07.

- [ ] RED: B không dùng được credential_ref của A; DB ciphertext không giải mã khi
  đổi owner/provider/version; refresh đồng thời cùng credential chỉ exchange một lần;
  secret không xuất hiện ở response, exception, argv, audit hoặc snapshot.
- [ ] Dùng AEAD library với key ID/nonce/associated data, key ngoài repo/DB; quy trình
  rotate/revoke, CSRF, one-use linking state; API chỉ trả trạng thái/scopes/project.
- [ ] Xóa ambient `.env`/global token fallback trong server adapter; local facade giữ
  hành vi cũ. CDSE chỉ bật capability đã qua R02; không lưu mật khẩu dài hạn tự phát.
- [ ] Chạy `.venv\Scripts\python.exe -m unittest tests.server.test_credential_vault tests.server.test_personal_connections tests.test_cdse -v`;
  kiểm thử key rotation và revoke queued/running policy với fake provider; checkpoint R05.

### R06 — Artifact riêng tư và download lease liên tiến trình

**Files:** Create `app/artifacts/{store,leases}.py`, `app/api/routes/artifacts.py`,
`tests/server/test_artifact_isolation.py`, `tests/integration/test_download_cleanup.py`;
modify frame/time-series/Esri output adapters và upload route.

**Interfaces:** `ArtifactStore.stage(ctx, job_id: UUID, attempt_id: UUID, epoch: int) -> StagingArea`;
`ArtifactStore.publish(lease: JobLease, files: list[ValidatedFile]) -> list[ArtifactRef]`;
`ArtifactStore.open(ctx, artifact_id: UUID) -> LeasedArtifact`;
`DownloadLease.heartbeat() -> bool`, context-managed close.
Types artifact ở `app/artifacts/store.py`; caller không được chỉ định filesystem path.

- [ ] RED: traversal/absolute filename/symlink escape, cross-user preview/ZIP/file,
  upload sai định dạng/quá kích thước, signed URL trong metadata bị chặn.
- [ ] Staging riêng từng attempt, validate raster cũ trước publish; file phải có
  owner/size/hash/provenance DB commit mới được tải. Implement download leases DB
  có heartbeat/expiry; root nằm ngoài static mount; không public directory listing.
- [ ] Test stream đang chạy khi process khác cleanup, client disconnect, API crash,
  lease hết hạn và orphan file chưa commit; reservation cleanup không tính hai lần.
- [ ] Chạy `.venv\Scripts\python.exe -m unittest tests.server.test_artifact_isolation tests.integration.test_download_cleanup tests.test_frame_export tests.test_rectangle_e2e -v`;
  integration stream/cleanup dùng hai process + PostgreSQL thật; checkpoint R06.

### R07 — Durable queue, admission và quota dùng chung

**Files:** Create `app/jobs/{models,repository,admission,scheduler}.py`,
`tests/integration/test_job_claims.py`, `tests/integration/test_job_admission.py`;
modify tables/migrations và `app/api/routes/operations.py` skeleton submit/status.

**Interfaces:** `JobLease(job_id: UUID, owner_id: UUID, attempt_id: UUID, epoch: int, worker_id: str, expires_at: datetime)`;
`submit(ctx, operation: OperationRequest, idempotency_key: str) -> JobView`;
`claim(worker_id: str) -> JobLease | None`; `heartbeat(lease: JobLease) -> bool`;
`finish(lease: JobLease, result: OperationResult) -> bool`;
`cancel(ctx, job_id: UUID) -> JobView`. Operation types ở R09, R07 dùng tối thiểu
allowlisted test operation nội bộ, không publish handler tùy ý ra HTTP.

- [ ] RED trên hai process: hai claim không cùng attempt, idempotency retry không
  nhân job/quota, khác payload cùng key trả 409, stale epoch không finish được.
- [ ] Implement transaction queue/claim/reservations, DB time và lease 90s/heartbeat
  15s; unique constraints owner/idempotency; scheduling FIFO/user + round-robin.
- [ ] Quota cấu hình theo spec, xét cả retained+reserved+workflow+free disk; 429 khi
  vượt admission có retry hint; cancel/retry/crash release reservation đúng một lần.
  Worker reclaim chỉ lease expired, không quét ngắt mọi running khi startup.
- [ ] Chạy `.venv\Scripts\python.exe -m unittest tests.integration.test_job_claims tests.integration.test_job_admission -v`;
  dùng barrier hai process và PostgreSQL, fault injection trước/sau commit; checkpoint R07.

### R08 — Worker tách process và kernel provider không dùng lẫn tài khoản

**Files:** Create `app/worker.py`, `app/jobs/{worker,handlers}.py`,
`tests/integration/test_worker_isolation.py`, `tests/integration/test_worker_recovery.py`;
modify provider adapters, frame/time-series/Esri local-service orchestration.

**Interfaces:** `run_worker(settings: Settings, worker_id: str) -> None`;
`run_attempt(lease: JobLease, runtime: WorkerRuntime) -> None`;
`execute_operation(operation: OperationRequest, context: ProviderExecutionContext) -> OperationResult`.
`WorkerRuntime`/`ProviderExecutionContext` ở `app/jobs/worker.py`, gồm owner,
credential version/project, monotonic deadline, cancellation và private staging.

- [ ] RED: A/B dùng hai fake credential identity qua child process search/preview/
  export; không import `main`, không EE init trong API; token refresh không lẫn user.
- [ ] Supervisor claim rồi tạo child mới bằng `spawn`; child resolve vault theo job
  owner, init GEE explicit một lần, đóng client và thoát sau attempt. Worker server
  không dùng `_timeseries_service`/`_frame_service` local thread executor.
- [ ] Inject chết child/supervisor, hết disk, upstream timeout/429/403, revoke/cancel
  lúc ghi và trước finalize; kill child theo deadline; stale epoch không publish.
  Không tự resume workflow không chứng minh idempotent; interrupted phải tạo retry rõ ràng.
- [ ] Chạy `.venv\Scripts\python.exe -m unittest tests.integration.test_worker_isolation tests.integration.test_worker_recovery -v`;
  sau fake smoke mới chạy R02 live smoke hai tài khoản; checkpoint R08 phân biệt kết quả.

### R09 — API v2 và adapter cho toàn bộ workflow hiện có

**Files:** Create `app/api/models/operations.py`, complete
`app/api/routes/{operations,connections,artifacts}.py`, `app/jobs/handlers.py`,
`tests/server/test_operation_api.py`, `tests/contracts/test_workflow_parity.py`;
modify route modules ở R01 và bootstrap profile policy.

**Interfaces:** `OperationRequest = Annotated[Union[ReferenceOperation, SearchOperation,
PreviewOperation, PreflightOperation, ExportOperation], Field(discriminator='kind')]`;
workflow allowlist theo spec. `OperationResult` chứa `snapshot_id`, metadata và
`ArtifactRef` theo loại, không có server path/secret; `JobView` có owner-safe
status/progress/error/result links. Types định nghĩa tại `app/api/models/operations.py`.

- [ ] RED: cross-owner links và credential/preflight mix, exact asset/platform/date,
  auxiliary S2 empty/subset, blocked quality/preflight, expired reference và EODAG
  unsupported đều đi đúng contract; queued không có download URL thành công giả.
- [ ] Thêm `POST /api/v2/operations` (202 + Location), `GET /api/v2/jobs/{id}`,
  `GET /api/v2/jobs/{id}/result`, `POST /api/v2/jobs/{id}/cancel`,
  `GET /api/v2/artifacts/{id}`, `POST /api/v2/references` (bounded upload + validation job).
  Result đang chờ trả 409, lỗi theo code/status cố định, không raw upstream body.
- [ ] Adapter lần lượt: optical/SAR GEE/DEM → catalog/S5P → frame/time series →
  CDSE ZIP/S3/bands/SAR crop → AWS crop/mosaic → Esri. Mỗi workflow có parity
  fixture trước bật; search/preview/preflight GEE cũng qua worker, không chỉ download.
- [ ] Chạy `.venv\Scripts\python.exe -m unittest tests.server.test_operation_api tests.contracts.test_workflow_parity tests.test_s2_auxiliary tests.test_rectangle_e2e tests.test_frame_export -v`;
  kiểm kê cả 56 legacy operation: local giữ, server protect/deny/delegate rõ ràng;
  không còn đường server sync nặng vượt quota. Checkpoint từng nhóm adapter R09.

### R10 — Tách UI và thêm phiên/kết nối/job riêng của từng người

**Files:** Modify `static/index.html`, `static/workspace.js`, `static/matched-frame.js`,
`static/selection-state.js`, `static/s5p.js`, `static/esri.js`, `static/s2-layers.js`;
create `static/app/{api-client,auth,bootstrap,optical,catalog,timeseries,sar,downloads}.js`,
`tests/test_auth_ui.cjs`, `tests/test_operation_ui.cjs`, `tests/browser/multiuser.spec.cjs`,
`tests/browser/package.json`, browser lockfile/config và test users fixture.

**Interfaces:** `ApiClient.request(path, {method, body, signal}) -> Promise<object>`
gửi session cookie và CSRF cho mutation; `submitOperation(request, idempotencyKey)`,
`pollJob(jobId, signal)`, `AuthState.subscribe(listener) -> unsubscribe`;
`clearPrivateState() -> void`. Giữ bridge `window.MatchedFrame`, S2Layers và
RectangleROI trong giai đoạn chuyển; API client chọn v1 local/v2 server theo config server.

- [ ] Characterize request/invalidation hiện tại rồi tách inline từng controller,
  chạy node test sau mỗi phần; không thiết kế lại bản đồ hoặc chuyển React scaffold.
- [ ] RED: session 401/logout/đổi user phải abort search/poll, clear private result,
  không restore job A trong phiên B, không lưu token browser; ROI draft và theme
  có chính sách giữ rõ ràng, riêng tư mặc định khi đổi user.
- [ ] Thêm kết nối provider/status/quota/queue/cancel/partial, async search/preview/
  preflight bằng v2; UI chỉ enable provider capability đã có. S2 selection tiếp tục
  có all/none/individual và chụp vào request, không đọc lại lúc job worker chạy.
- [ ] Chạy `node --test tests/*.cjs`; browser package riêng dùng Playwright đã lock,
  `npm --prefix tests/browser ci` rồi `npm --prefix tests/browser test` với
  `MULTIUSER_BASE_URL` môi trường isolated + fake IdP/provider. Hai browser context,
  bàn phím, 375px và desktop; capture screenshot/console/network đã bỏ secrets.
  Checkpoint R10; fake auth không chứng minh callback/provider thật.

### R11 — Migration dữ liệu local có dry-run và rollback

**Files:** Create `app/artifacts/migration.py`, `scripts/migrate_local_data.py`,
`tests/integration/test_local_migration.py`, `docs/operations/multiuser-migration.md`;
read SQLite/JSON/downloads theo allowlist, không sửa nguồn.

**Interfaces:** `inventory_local(root: Path) -> MigrationInventory`;
`plan_import(inventory, owner_map: dict[str, UUID]) -> MigrationPlan`;
`apply_import(plan: MigrationPlan, *, dry_run: bool = True) -> MigrationReport`;
types ở `app/artifacts/migration.py`; yêu cầu maintenance identity ở ngoài HTTP.

- [ ] RED: thiếu owner mapping, orphan/corrupt files, digest/hash mismatch, symlink,
  snapshot quá cũ/không rõ ROI version bị quarantine; không tự gán cho user đầu tiên.
- [ ] Drain active jobs, backup SQLite/JSON/files, dry-run số lượng+size+hash+conflict,
  copy artifact private và metadata mới, giữ nguyên payload bytes/digest lịch sử;
  import idempotent không duplicate và không kích hoạt job cũ chạy lại.
- [ ] Rehearsal với bản copy dữ liệu đã được cho phép, kiểm raster/owner/manifests;
  không chạm credentials `.env`/key file hoặc copy sang image/container.
- [ ] Chạy `.venv\Scripts\python.exe -m unittest tests.integration.test_local_migration -v`;
  diễn tập restore nguồn và trỏ lại local profile; giữ R11 report/rollback checklist.

### R12 — Vận hành server, observability và dependency reproducibility

**Files:** Create `deploy/{compose.yaml,proxy.conf,Dockerfile.api,Dockerfile.worker}`,
`.dockerignore`, `app/observability/{logging,metrics,audit}.py`,
`app/api/routes/health.py`, `.github/workflows/server-tests.yml`,
`tests/server/test_deployment_policy.py`, `docs/operations/multiuser-runbook.md`;
modify `.env.example`, `README.md`, locked server requirements ở R02.

**Interfaces:** `health_live() -> dict`, `health_ready(services: AppServices) -> dict`;
`record_event(ctx, action: str, resource_id: UUID | None, outcome: str) -> None`;
metrics labels không có raw owner/ROI/token/URL. Maintenance leader lease dùng DB.

- [ ] RED: server thiếu key/DB/OIDC/origin fail startup, untrusted forwarded header
  không đổi cookie/callback origin, 2 API startup không recover job, secret sentinel
  không xuất hiện log/audit/error; readiness không gọi GEE/CDSE và không lộ config.
- [ ] Compose một host: proxy TLS, PostgreSQL, 2 API replica và 2 worker replica,
  private volume, non-root app containers, resource cap từng worker, shutdown/drain,
  migration chạy một lần có kiểm soát. Không mount repo/key/.env vào image build context.
- [ ] CI tách base offline, PostgreSQL integration, browser fixture và optional live;
  dependency scan trên lockfile, server image/GDAL smoke, backup DB+volume+vault key,
  restore và key rotation drill. Không tự sửa pin để né dependency failure.
- [ ] Chạy `.venv\Scripts\python.exe -m unittest tests.server.test_deployment_policy -v`,
  `docker compose -f deploy/compose.yaml config --quiet`, build/start isolated stack,
  health + backup/restore drill. Không in compose config đã nội suy secrets.
  Checkpoint R12 có CPU/RAM/đĩa/OS/image digest và rollback instructions.

### R13 — Nghiệm thu nhiều người dùng và bàn giao

**Files:** Create `scripts/accept_multiuser.py`, `tests/integration/test_multiuser_acceptance.py`,
`docs/evidence/multiuser-refactor/acceptance.md`; update README/runbook.

**Interfaces:** `run_acceptance(base_url: str, scenario: AcceptanceScenario) -> AcceptanceReport`;
scenario/report ở `scripts/accept_multiuser.py`, redacted IDs và source/image/schema hash.
Không có cờ cho phép đánh dấu passed khi thiếu DB/process/live evidence.

- [ ] Chạy toàn bộ Python + node regression và browser fixture suite; ghi từng
  skip/failure, local vs server. Chưa cần chạy scaffold NDVI ngoài phạm vi refactor.
- [ ] Hai API + hai worker + PostgreSQL thật: A/B isolation mọi resource, POST retry,
  quota/fairness, kill/restart/cancel/revoke, stale finalize, cleanup/download race.
  Fake provider load 10 phiên/100 operation; đo p95 status ≤500ms, submit ≤1s,
  không vượt 2 running toàn hệ thống/1 running/user hoặc 5 queued/user.
- [ ] Live hai tài khoản GEE/CDSE được phép: kiểm identity/project đúng, refresh/
  revoke, một export nhỏ mỗi provider capability; đo raster grid/bands/scale/mask
  và auth separation. Provider không khả dụng phải hiện rõ unsupported/blocked.
- [ ] Rehearsal migration/backup/restore/rollback trên server OS đề xuất và local
  Windows; mới đánh dấu server ready khi mọi gate bắt buộc đạt. Bàn giao release
  checklist, giới hạn công suất đã đo, capability chưa bật và người vận hành phụ trách.

## 4. Quy tắc RED/GREEN, dừng và tiếp tục

- Refactor thuần: characterization và parity giữ GREEN qua từng lát; không đổi
  expected chỉ vì chuyển file. Capability mới: viết RED trước, xác nhận lỗi đúng
  hành vi thiếu, implement rồi GREEN. Import/dependency/DB unavailable không phải TDD RED.
- Thiếu provider OAuth/client/quyền hoặc test DB: ghi task/gate cụ thể; vẫn làm
  phần độc lập, không chuyển task phụ thuộc sang completed, không bỏ test để qua.
- Mỗi checkpoint ghi source hash, task/substep, files đã đổi, lệnh/exit/count/skip,
  migration version, dependency và bước tiếp theo. Trước resume kiểm tra branch,
  dirty tree, source hash và evidence; chỉ chạy lại phần bị ảnh hưởng.
- Khi lỗi parity raster: dừng mở rộng task đó, quay adapter của lát vừa đổi về
  đường cũ trong local profile; giữ file/dữ liệu cũ. Không reset toàn bộ working tree.
- Rollback server: đóng admission, drain/cancel có kiểm soát, giữ snapshot/DB/private
  volume, rollback app image tương thích schema expand/contract; schema destructive
  chỉ sau release riêng có backup. Không trỏ code cũ vào schema mới chưa kiểm chứng.
- Tài liệu này không cho phép triển khai public, dùng credentials thật ngoài smoke
  được phép, thay auth/CORS production, tự commit/push hoặc triển khai tính năng NDVI.

## 5. Thứ tự khuyến nghị khi bắt đầu thực hiện

Thực hiện R00 rồi R01 theo từng lát nhỏ; làm R02 sớm để phát hiện ràng buộc OAuth.
Không tăng Uvicorn workers hoặc mở server cho nhiều người trước R03–R09. Sau khi
duyệt thiết kế, điều chỉnh cấu hình máy/IdP/capacity vào spec và plan cùng lúc;
không thay nguyên tắc owner/process isolation để rút ngắn công việc.
