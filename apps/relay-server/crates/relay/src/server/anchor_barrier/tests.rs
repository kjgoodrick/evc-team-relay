use super::*;
use crate::stores::filesystem::FileSystemStore;
use async_trait::async_trait;
use axum::body::Body;
use std::sync::atomic::{AtomicBool, Ordering};
use tower::ServiceExt;
use y_sweet_core::store::{Result as StoreResult, StoreError};

const DOC: &str = "00000000-0000-4000-8000-000000000001";
const SHARE_ID: &str = "00000000-0000-4000-8000-000000000002";
const GEN: &str = "00000000-0000-4000-8000-000000000003";
const REQUEST_ID: &str = "00000000-0000-4000-8000-000000000004";

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct Fixture {
    name: String,
    update: Vec<u8>,
    dependency: Vec<u8>,
    start: Vec<u8>,
    end: Vec<u8>,
    selected_length: u32,
    expected: Value,
}
fn vectors() -> Vec<Fixture> {
    let value: Value = serde_json::from_str(include_str!(
        "../../../../y-sweet-core/tests/fixtures/yjs-text-anchors-v1.json"
    ))
    .unwrap();
    serde_json::from_value(value["fixtures"].clone()).unwrap()
}
fn envelope(doc: &str, fixture: Option<&Fixture>) -> Value {
    let mut value = json!({ "protocol_version": 1, "request_id": REQUEST_ID, "document_id": doc,
        "share_id": SHARE_ID, "generation": GEN, "expires_at_ms": current_time_epoch_millis() + 25_000 });
    if let Some(fixture) = fixture {
        value["anchors"] = json!([{ "dependency": BASE64.encode(&fixture.dependency), "start": BASE64.encode(&fixture.start),
            "end": BASE64.encode(&fixture.end), "selected_length": fixture.selected_length }]);
    }
    value
}
async fn raw(server: &Arc<Server>, token: &str, route: &str, body: Vec<u8>) -> (StatusCode, Value) {
    let response = server
        .routes()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(format!("/internal/collaboration/{route}"))
                .header("authorization", format!("Bearer {token}"))
                .body(Body::from(body))
                .unwrap(),
        )
        .await
        .unwrap();
    let status = response.status();
    if status.is_success() {
        assert_eq!(response.headers()["cache-control"], "no-store");
    }
    let bytes = to_bytes(response.into_body(), MAX_BODY).await.unwrap();
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}
async fn call(server: &Arc<Server>, token: &str, route: &str, body: Value) -> (StatusCode, Value) {
    raw(server, token, route, serde_json::to_vec(&body).unwrap()).await
}
fn auth() -> Authenticator {
    let mut auth = Authenticator::gen_key().unwrap();
    auth.set_expected_audience(Some("https://relay.test".into()));
    auth
}
async fn server(store: Option<Box<dyn Store>>, enabled: bool) -> (Arc<Server>, String, String) {
    let auth = auth();
    let token = auth.server_token().unwrap();
    let document_token = auth
        .gen_doc_token_cwt(
            DOC,
            Authorization::Full,
            ExpirationTimeEpochMillis(current_time_epoch_millis() + 60_000),
            None,
            Some(SHARE_ID.into()),
        )
        .unwrap();
    let server = Server::new(
        store,
        Duration::from_secs(60),
        Some(auth),
        None,
        vec![],
        CancellationToken::new(),
        false,
        None,
    )
    .await
    .unwrap()
    .with_anchor_barriers(enabled)
    .unwrap();
    (Arc::new(server), token, document_token)
}
async fn load(server: &Server, doc: &str, fixture: &Fixture) {
    server
        .load_doc_with_user(doc, Some(SHARE_ID.into()), None)
        .await
        .unwrap();
    server
        .docs
        .get(doc)
        .unwrap()
        .apply_update(&fixture.update)
        .unwrap();
}

#[tokio::test]
async fn existing_managed_document_binds_without_changing_its_routing() {
    let temp = tempfile::tempdir().unwrap();
    let (server, token, _) = server(
        Some(Box::new(
            FileSystemStore::new(temp.path().to_owned()).unwrap(),
        )),
        true,
    )
    .await;
    let fixture = &vectors()[0];
    server.load_doc_with_user(DOC, None, None).await.unwrap();
    server
        .docs
        .get(DOC)
        .unwrap()
        .apply_update(&fixture.update)
        .unwrap();
    assert_eq!(server.docs.get(DOC).unwrap().get_channel(), None);
    assert_eq!(
        call(&server, &token, "register", envelope(DOC, None))
            .await
            .0,
        StatusCode::OK
    );
    assert_eq!(
        call(&server, &token, "validate", envelope(DOC, Some(fixture)))
            .await
            .0,
        StatusCode::OK
    );
    assert_eq!(server.docs.get(DOC).unwrap().get_channel(), None);
    let mut foreign = envelope(DOC, None);
    foreign["share_id"] = json!(REQUEST_ID);
    assert_eq!(
        call(&server, &token, "register", foreign).await.0,
        StatusCode::CONFLICT
    );
    assert!(!server.docs.contains_key(SHARE_ID));
    server.drain_workers().await;
}

#[tokio::test]
async fn private_routes_are_disabled_by_default_and_require_server_authority() {
    let temp = tempfile::tempdir().unwrap();
    let (disabled, token, _) = server(
        Some(Box::new(
            FileSystemStore::new(temp.path().to_owned()).unwrap(),
        )),
        false,
    )
    .await;
    assert_eq!(
        call(&disabled, &token, "register", envelope(DOC, None))
            .await
            .0,
        StatusCode::NOT_FOUND
    );
    disabled.drain_workers().await;
    let (enabled, token, document_token) = server(
        Some(Box::new(
            FileSystemStore::new(temp.path().to_owned()).unwrap(),
        )),
        true,
    )
    .await;
    for token in ["", "invalid", &document_token] {
        // Authorization runs before decoding malformed/oversized input.
        assert_eq!(
            raw(&enabled, token, "validate", vec![b'x'; MAX_BODY + 1])
                .await
                .0,
            StatusCode::UNAUTHORIZED
        );
        assert_eq!(
            raw(&enabled, token, "source", vec![b'x'; MAX_BODY + 1])
                .await
                .0,
            StatusCode::UNAUTHORIZED
        );
    }
    assert_eq!(
        call(&enabled, &token, "register", envelope(DOC, None))
            .await
            .0,
        StatusCode::NOT_FOUND
    );
    assert!(!temp.path().join(DOC).exists());
    assert!(enabled.docs.is_empty());
    enabled.drain_workers().await;
    assert!(Server::new(
        None,
        Duration::from_secs(60),
        Some(auth()),
        None,
        vec![],
        CancellationToken::new(),
        false,
        None
    )
    .await
    .unwrap()
    .with_anchor_barriers(true)
    .is_err());
    assert!(Server::new(
        Some(Box::new(
            FileSystemStore::new(temp.path().to_owned()).unwrap()
        )),
        Duration::from_secs(60),
        None,
        None,
        vec![],
        CancellationToken::new(),
        false,
        None
    )
    .await
    .unwrap()
    .with_anchor_barriers(true)
    .is_err());
}

async fn load_source(server: &Server, source: &str) -> Value {
    use yrs::{Doc, OffsetKind, Options, ReadTxn, StateVector, Text, Transact};
    let doc = Doc::with_options(Options {
        offset_kind: OffsetKind::Utf16,
        ..Options::default()
    });
    let text = doc.get_or_insert_text("contents");
    text.insert(&mut doc.transact_mut(), 0, source);
    let dependency = doc.transact().snapshot().encode_v1();
    let update = doc
        .transact()
        .encode_state_as_update_v1(&StateVector::default());
    server
        .load_doc_with_user(DOC, Some(SHARE_ID.into()), None)
        .await
        .unwrap();
    server.docs.get(DOC).unwrap().apply_update(&update).unwrap();
    let mut request = envelope(DOC, None);
    request["source_dependency"] = json!(BASE64.encode(&dependency));
    request
}

#[tokio::test]
async fn source_observations_bind_generation_and_hashes_and_survive_filesystem_reload() {
    let temp = tempfile::tempdir().unwrap();
    let (server, token, _) = server(
        Some(Box::new(
            FileSystemStore::new(temp.path().to_owned()).unwrap(),
        )),
        true,
    )
    .await;
    let text = "---\nstatus: Planned 🌿\n---\nPrivate body";
    let request = load_source(&server, text).await;
    assert_eq!(
        call(&server, &token, "source", request.clone()).await.1["code"],
        "generation_mismatch"
    );
    assert_eq!(
        call(&server, &token, "register", envelope(DOC, None))
            .await
            .0,
        StatusCode::OK
    );
    for _ in 0..2 {
        let (status, response) = call(&server, &token, "source", request.clone()).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(response["source_prefix"], text);
        assert_eq!(response["source_complete"], true);
        assert_eq!(
            response["source_prefix_sha256"],
            hex::encode(Sha256::digest(text.as_bytes()))
        );
        assert_eq!(
            response["request_sha256"],
            hex::encode(Sha256::digest(serde_json::to_vec(&request).unwrap()))
        );
        assert_eq!(
            response["observed_snapshot_sha256"].as_str().unwrap().len(),
            64
        );
        assert!(response.get("placements").is_none());
        assert!(
            Server::evict_unused_doc(&server.docs, &server.loading_locks, DOC)
                .await
                .unwrap()
        );
    }
    let mut foreign = request.clone();
    foreign["generation"] = json!(REQUEST_ID);
    assert_eq!(
        call(&server, &token, "source", foreign).await.1["code"],
        "generation_mismatch"
    );
    let mut foreign = request;
    foreign["share_id"] = json!(REQUEST_ID);
    assert_eq!(
        call(&server, &token, "source", foreign).await.1["code"],
        "share_mismatch"
    );
    server.drain_workers().await;
}

#[tokio::test]
async fn source_requests_cannot_mix_protocols_allocate_from_forged_counts_or_create_unknown_notes()
{
    let temp = tempfile::tempdir().unwrap();
    let (server, token, _) = server(
        Some(Box::new(
            FileSystemStore::new(temp.path().to_owned()).unwrap(),
        )),
        true,
    )
    .await;
    let mut request = envelope(DOC, None);
    assert_eq!(
        call(&server, &token, "source", request.clone()).await.0,
        StatusCode::UNPROCESSABLE_ENTITY
    );
    request["source_dependency"] = json!(BASE64.encode(&[0, 0]));
    assert_eq!(
        call(&server, &token, "source", request.clone()).await.0,
        StatusCode::NOT_FOUND
    );
    assert!(server.docs.is_empty());
    assert!(!temp.path().join(DOC).exists());
    for route in ["register", "validate"] {
        assert_eq!(
            call(&server, &token, route, request.clone()).await.0,
            StatusCode::UNPROCESSABLE_ENTITY
        );
    }
    let mut mixed = request.clone();
    mixed["anchors"] = envelope(DOC, Some(&vectors()[0]))["anchors"].clone();
    assert_eq!(
        call(&server, &token, "source", mixed).await.1["code"],
        "invalid_batch"
    );
    for encoded in [
        BASE64.encode(&[0, 255, 255, 255, 255, 15]),
        BASE64.encode(&[0, 0, 42]),
        "AA".into(),
        "A".repeat(90000),
    ] {
        request["source_dependency"] = json!(encoded);
        assert_eq!(
            call(&server, &token, "source", request.clone()).await.0,
            StatusCode::UNPROCESSABLE_ENTITY
        );
    }
    server.drain_workers().await;
}

#[tokio::test]
async fn source_prefix_caps_are_explicit_and_checkpoint_failure_returns_no_source() {
    use y_sweet_core::source::{MAX_SOURCE_DOCUMENT_UTF16, MAX_SOURCE_PREFIX_BYTES};
    let temp = tempfile::tempdir().unwrap();
    let files = ControlledStore::new(temp.path().to_owned());
    let (server, token, _) = server(Some(Box::new(DurableTestStore(files.clone()))), true).await;
    let request = load_source(
        &server,
        &("x".repeat(MAX_SOURCE_PREFIX_BYTES - 1) + "🌿tail"),
    )
    .await;
    assert_eq!(
        call(&server, &token, "register", envelope(DOC, None))
            .await
            .0,
        StatusCode::OK
    );
    let (status, value) = call(&server, &token, "source", request.clone()).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        value["source_prefix"].as_str().unwrap().len(),
        MAX_SOURCE_PREFIX_BYTES - 1
    );
    assert_eq!(value["source_complete"], false);
    // Force an actual ordered write so a failing store cannot issue a proof.
    server
        .docs
        .get(DOC)
        .unwrap()
        .sync_kv()
        .update_metadata("source-test".into(), ciborium::value::Value::Bool(true));
    files.fail_next.store(true, Ordering::SeqCst);
    let (status, value) = call(&server, &token, "source", request).await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
    assert!(value.get("source_prefix").is_none());
    // Insert a second client's content, preserving the bound document lineage.
    let request = load_source(&server, &"x".repeat(MAX_SOURCE_DOCUMENT_UTF16 as usize + 1)).await;
    let (status, value) = call(&server, &token, "source", request).await;
    assert_eq!(status, StatusCode::PAYLOAD_TOO_LARGE);
    assert_eq!(value["code"], "source_document_too_large");
    assert!(value.get("source_prefix").is_none());
    server.drain_workers().await;
}

#[tokio::test]
async fn golden_anchors_cross_private_routes_and_filesystem_reload() {
    let temp = tempfile::tempdir().unwrap();
    let (server, token, _) = server(
        Some(Box::new(
            FileSystemStore::new(temp.path().to_owned()).unwrap(),
        )),
        true,
    )
    .await;
    for (i, fixture) in vectors().iter().enumerate() {
        let doc = format!("00000000-0000-4000-8000-{:012}", i + 10);
        load(&server, &doc, fixture).await;
        assert_eq!(
            call(&server, &token, "register", envelope(&doc, None))
                .await
                .0,
            StatusCode::OK
        );
        let request = envelope(&doc, Some(fixture));
        let expected_status = match fixture.expected["status"].as_str().unwrap() {
            "waiting_for_sync" => StatusCode::CONFLICT,
            "invalid" => StatusCode::UNPROCESSABLE_ENTITY,
            _ => StatusCode::OK,
        };
        for _ in 0..2 {
            let (status, receipt) = call(&server, &token, "validate", request.clone()).await;
            assert_eq!(status, expected_status, "{}", fixture.name);
            if status.is_success() {
                assert_eq!(
                    receipt["placements"][0], fixture.expected,
                    "{}",
                    fixture.name
                );
                assert_eq!(
                    receipt["request_sha256"],
                    hex::encode(Sha256::digest(serde_json::to_vec(&request).unwrap()))
                );
                assert_eq!(receipt["generation"], GEN);
                assert_eq!(receipt["document_id"], doc);
                assert_eq!(
                    receipt["observed_snapshot_sha256"].as_str().unwrap().len(),
                    64
                );
            }
            assert!(
                Server::evict_unused_doc(&server.docs, &server.loading_locks, &doc)
                    .await
                    .unwrap()
            );
        }
    }
    server.drain_workers().await;
}

#[tokio::test]
async fn request_limits_generations_and_share_bindings_fail_closed() {
    let temp = tempfile::tempdir().unwrap();
    let fixture = vectors().remove(0);
    let (server, token, _) = server(
        Some(Box::new(
            FileSystemStore::new(temp.path().to_owned()).unwrap(),
        )),
        true,
    )
    .await;
    load(&server, DOC, &fixture).await;
    assert_eq!(
        call(&server, &token, "validate", envelope(DOC, Some(&fixture)))
            .await
            .1["code"],
        "generation_mismatch"
    );
    assert_eq!(
        call(&server, &token, "register", envelope(DOC, None))
            .await
            .0,
        StatusCode::OK
    );
    let mut value = envelope(DOC, None);
    value["generation"] = json!(REQUEST_ID);
    assert_eq!(
        call(&server, &token, "register", value).await.1["code"],
        "generation_mismatch"
    );
    let mut value = envelope(DOC, Some(&fixture));
    value["share_id"] = json!(REQUEST_ID);
    assert_eq!(
        call(&server, &token, "validate", value).await.1["code"],
        "share_mismatch"
    );
    for expiry in [0, current_time_epoch_millis() + MAX_LIFETIME_MS + 10_000] {
        let mut value = envelope(DOC, Some(&fixture));
        value["expires_at_ms"] = json!(expiry);
        assert_eq!(
            call(&server, &token, "validate", value).await.0,
            StatusCode::REQUEST_TIMEOUT
        );
    }
    let mut value = envelope("../outside", Some(&fixture));
    assert_eq!(
        call(&server, &token, "validate", value.clone()).await.0,
        StatusCode::UNPROCESSABLE_ENTITY
    );
    value["document_id"] = json!(DOC);
    value["anchors"][0]["dependency"] = json!(BASE64.encode(&vec![0; 65537]));
    assert_eq!(
        call(&server, &token, "validate", value).await.0,
        StatusCode::UNPROCESSABLE_ENTITY
    );
    assert_eq!(
        raw(&server, &token, "validate", vec![b' '; MAX_BODY + 1])
            .await
            .0,
        StatusCode::PAYLOAD_TOO_LARGE
    );
    let slots = server
        .anchor_barrier_slots
        .clone()
        .acquire_many_owned(4)
        .await
        .unwrap();
    assert_eq!(
        call(&server, &token, "validate", envelope(DOC, Some(&fixture)))
            .await
            .0,
        StatusCode::TOO_MANY_REQUESTS
    );
    drop(slots);
    server.drain_workers().await;
}

struct ControlledStore {
    files: FileSystemStore,
    pause_next: AtomicBool,
    fail_next: AtomicBool,
    entered: tokio::sync::Notify,
    release: tokio::sync::Semaphore,
}

#[test]
fn decoded_batch_and_position_bounds_are_independent_of_json_size() {
    let fixture = vectors().remove(0);
    let mut value = envelope(DOC, Some(&fixture));
    value["anchors"][0]["start"] = json!(BASE64.encode(&vec![0; 257]));
    assert!(serde_json::from_value::<Envelope>(value)
        .unwrap()
        .validate(false)
        .is_err());
    let mut value = envelope(DOC, Some(&fixture));
    let mut large = value["anchors"][0].clone();
    large["dependency"] = json!(BASE64.encode(&vec![0; 64 * 1024]));
    value["anchors"] = json!([large.clone(), large.clone(), large.clone(), large]);
    assert!(serde_json::to_vec(&value).unwrap().len() < MAX_BODY);
    assert_eq!(
        serde_json::from_value::<Envelope>(value)
            .unwrap()
            .validate(false)
            .unwrap_err()
            .1
             .0["code"],
        "batch_too_large"
    );
    let mut value = envelope(DOC, Some(&fixture));
    value["anchors"] = json!(vec![value["anchors"][0].clone(); 33]);
    assert!(serde_json::from_value::<Envelope>(value)
        .unwrap()
        .validate(false)
        .is_err());
}
impl ControlledStore {
    fn new(path: std::path::PathBuf) -> Arc<Self> {
        Arc::new(Self {
            files: FileSystemStore::new(path).unwrap(),
            pause_next: AtomicBool::new(false),
            fail_next: AtomicBool::new(false),
            entered: tokio::sync::Notify::new(),
            release: tokio::sync::Semaphore::new(0),
        })
    }
}
struct DurableTestStore(Arc<ControlledStore>);
impl std::ops::Deref for DurableTestStore {
    type Target = ControlledStore;
    fn deref(&self) -> &Self::Target {
        &self.0
    }
}
#[async_trait]
impl Store for DurableTestStore {
    fn supports_durable_checkpoints(&self) -> bool {
        true
    }
    async fn init(&self) -> StoreResult<()> {
        Ok(())
    }
    async fn get(&self, key: &str) -> StoreResult<Option<Vec<u8>>> {
        self.files.get(key).await
    }
    async fn exists(&self, key: &str) -> StoreResult<bool> {
        self.files.exists(key).await
    }
    async fn remove(&self, key: &str) -> StoreResult<()> {
        self.files.remove(key).await
    }
    async fn set(&self, key: &str, value: Vec<u8>) -> StoreResult<()> {
        if self.pause_next.swap(false, Ordering::SeqCst) {
            self.entered.notify_one();
            self.release.acquire().await.unwrap().forget();
        }
        if self.fail_next.swap(false, Ordering::SeqCst) {
            return Err(StoreError::ConnectionError("injected".into()));
        }
        self.files.set(key, value).await
    }
}

#[tokio::test]
async fn disconnected_request_keeps_checkpoint_owned_and_shutdown_drains_it() {
    let temp = tempfile::tempdir().unwrap();
    let files = ControlledStore::new(temp.path().to_owned());
    let fixture = vectors().remove(0);
    let (server, token, _) = server(Some(Box::new(DurableTestStore(files.clone()))), true).await;
    load(&server, DOC, &fixture).await;
    assert_eq!(
        call(&server, &token, "register", envelope(DOC, None))
            .await
            .0,
        StatusCode::OK
    );
    files.pause_next.store(true, Ordering::SeqCst);
    server.docs.get(DOC).unwrap().sync_kv().update_metadata(
        "test".into(),
        ciborium::value::Value::Text("pending".into()),
    );
    let request = envelope(DOC, Some(&fixture));
    let calling = {
        let server = server.clone();
        let token = token.clone();
        let request = request.clone();
        tokio::spawn(async move { call(&server, &token, "validate", request).await })
    };
    tokio::time::timeout(Duration::from_secs(2), files.entered.notified())
        .await
        .unwrap();
    calling.abort();
    let _ = calling.await;
    assert_eq!(server.anchor_barrier_slots.available_permits(), 3);
    assert_eq!(
        call(&server, &token, "validate", request).await.1["code"],
        "document_busy"
    );
    let draining = {
        let server = server.clone();
        tokio::spawn(async move { server.drain_workers().await })
    };
    tokio::task::yield_now().await;
    assert!(!draining.is_finished());
    files.release.add_permits(1);
    tokio::time::timeout(Duration::from_secs(2), draining)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(server.anchor_barrier_slots.available_permits(), 4);
    let reloaded = DocWithSyncKv::new(
        DOC,
        Some(Arc::new(Box::new(DurableTestStore(files.clone())))),
        || {},
        None,
    )
    .await
    .unwrap();
    assert_eq!(
        reloaded.sync_kv().get_metadata().unwrap().get("test"),
        Some(&ciborium::value::Value::Text("pending".into()))
    );
}

#[tokio::test]
async fn failed_checkpoint_never_returns_a_receipt_and_retry_saves_state() {
    let temp = tempfile::tempdir().unwrap();
    let files = ControlledStore::new(temp.path().to_owned());
    let (server, token, _) = server(Some(Box::new(DurableTestStore(files.clone()))), true).await;
    let fixture = vectors().remove(0);
    load(&server, DOC, &fixture).await;
    files.fail_next.store(true, Ordering::SeqCst);
    let failed = call(&server, &token, "register", envelope(DOC, None)).await;
    assert_eq!(failed.0, StatusCode::SERVICE_UNAVAILABLE);
    assert!(failed.1.get("request_sha256").is_none());
    assert_eq!(
        call(&server, &token, "register", envelope(DOC, None))
            .await
            .0,
        StatusCode::OK
    );
    assert_eq!(
        call(&server, &token, "validate", envelope(DOC, Some(&fixture)))
            .await
            .0,
        StatusCode::OK
    );
    server.drain_workers().await;
}

#[tokio::test]
async fn expired_wait_does_not_cancel_storage_or_issue_a_late_receipt() {
    let temp = tempfile::tempdir().unwrap();
    let files = ControlledStore::new(temp.path().to_owned());
    let (server, token, _) = server(Some(Box::new(DurableTestStore(files.clone()))), true).await;
    let fixture = vectors().remove(0);
    load(&server, DOC, &fixture).await;
    files.pause_next.store(true, Ordering::SeqCst);
    let mut request = envelope(DOC, None);
    request["expires_at_ms"] = json!(current_time_epoch_millis() + 250);
    let waiting = {
        let server = server.clone();
        let token = token.clone();
        tokio::spawn(async move { call(&server, &token, "register", request).await })
    };
    tokio::time::timeout(Duration::from_secs(2), files.entered.notified())
        .await
        .unwrap();
    let result = tokio::time::timeout(Duration::from_secs(2), waiting)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(result.0, StatusCode::REQUEST_TIMEOUT);
    assert!(result.1.get("request_sha256").is_none());
    assert_eq!(server.anchor_barrier_slots.available_permits(), 3);
    files.release.add_permits(1);
    server.drain_workers().await;
    assert_eq!(server.anchor_barrier_slots.available_permits(), 4);
    let reloaded = DocWithSyncKv::new(
        DOC,
        Some(Arc::new(Box::new(DurableTestStore(files)))),
        || {},
        None,
    )
    .await
    .unwrap();
    assert_eq!(
        reloaded.sync_kv().get_metadata().unwrap().get(GENERATION),
        Some(&ciborium::value::Value::Text(GEN.into()))
    );
}

#[tokio::test]
async fn eviction_blocks_reload_until_final_checkpoint_and_keeps_live_doc_on_failure() {
    let temp = tempfile::tempdir().unwrap();
    let files = ControlledStore::new(temp.path().to_owned());
    let (server, _, _) = server(Some(Box::new(DurableTestStore(files.clone()))), true).await;
    let fixture = vectors().remove(0);
    load(&server, DOC, &fixture).await;
    let kv = server.docs.get(DOC).unwrap().sync_kv();
    files.fail_next.store(true, Ordering::SeqCst);
    assert!(
        Server::evict_unused_doc(&server.docs, &server.loading_locks, DOC)
            .await
            .is_err()
    );
    assert!(Arc::ptr_eq(&kv, &server.docs.get(DOC).unwrap().sync_kv()));
    files.pause_next.store(true, Ordering::SeqCst);
    let evicting = {
        let server = server.clone();
        tokio::spawn(async move {
            Server::evict_unused_doc(&server.docs, &server.loading_locks, DOC).await
        })
    };
    tokio::time::timeout(Duration::from_secs(2), files.entered.notified())
        .await
        .unwrap();
    assert!(!server.docs.contains_key(DOC));
    let loading = {
        let server = server.clone();
        tokio::spawn(async move { server.load_doc(DOC, None).await })
    };
    tokio::task::yield_now().await;
    assert!(!loading.is_finished());
    files.release.add_permits(1);
    assert!(evicting.await.unwrap().unwrap());
    loading.await.unwrap().unwrap();
    let input = serde_json::from_value::<Envelope>(envelope(DOC, Some(&fixture)))
        .unwrap()
        .validate(false)
        .unwrap();
    assert_eq!(
        serde_json::to_value(
            &server
                .docs
                .get(DOC)
                .unwrap()
                .resolve_text_anchors(&input)
                .unwrap()[0]
        )
        .unwrap(),
        fixture.expected
    );
    server.drain_workers().await;
}
