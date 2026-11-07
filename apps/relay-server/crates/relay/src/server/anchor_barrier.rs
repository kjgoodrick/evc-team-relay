//! Private, bounded observations of durable managed text. These responses are
//! trusted only over the authenticated control-plane/relay hop, never as a
//! client-supplied proof. No ordinary document credential is accepted.
use super::*;
use axum::body::to_bytes;
use data_encoding::BASE64;
use serde::{Deserialize, Serialize};
use y_sweet_core::anchors::{resolve_with_snapshot, AnchorPlacement, TextAnchorInput};
use y_sweet_core::source::{decode_source_dependency, observe_source, SourceObservation};
use yrs::updates::encoder::Encode;
use yrs::Snapshot;

const MAX_BODY: usize = 512 * 1024;
const MAX_LIFETIME_MS: u64 = 30_000;
const GENERATION: &str = "collaboration_generation";
const SHARE: &str = "collaboration_share_id";
type Failure = (StatusCode, Json<Value>);

fn failure(status: StatusCode, code: &'static str) -> Failure {
    (status, Json(json!({ "code": code })))
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Envelope {
    protocol_version: u8,
    request_id: String,
    document_id: String,
    share_id: String,
    generation: String,
    expires_at_ms: u64,
    #[serde(default)]
    anchors: Vec<WireAnchor>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    source_dependency: Option<String>,
}

#[derive(Clone, Copy)]
enum Operation {
    Register,
    Anchors,
    Source,
}

enum Input {
    Register,
    Anchors(Vec<TextAnchorInput>),
    Source(Snapshot),
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct WireAnchor {
    dependency: String,
    start: String,
    end: String,
    selected_length: u32,
}

fn uuid(value: &str) -> bool {
    value.len() == 36
        && value.bytes().enumerate().all(|(i, byte)| {
            if [8, 13, 18, 23].contains(&i) {
                byte == b'-'
            } else {
                byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte)
            }
        })
}

fn decode(value: &str, limit: usize) -> Result<Vec<u8>, Failure> {
    let bad = || failure(StatusCode::UNPROCESSABLE_ENTITY, "invalid_anchor");
    if value.len() > limit.div_ceil(3) * 4 {
        return Err(bad());
    }
    let bytes = BASE64.decode(value.as_bytes()).map_err(|_| bad())?;
    if bytes.len() > limit || BASE64.encode(&bytes) != value {
        return Err(bad());
    }
    Ok(bytes)
}

impl Envelope {
    fn validate_header(&self) -> Result<(), Failure> {
        if self.protocol_version != 1
            || ![
                &self.request_id,
                &self.document_id,
                &self.share_id,
                &self.generation,
            ]
            .into_iter()
            .all(|id| uuid(id))
        {
            return Err(failure(StatusCode::UNPROCESSABLE_ENTITY, "invalid_request"));
        }
        let now = current_time_epoch_millis();
        if self.expires_at_ms <= now || self.expires_at_ms > now.saturating_add(MAX_LIFETIME_MS) {
            return Err(failure(StatusCode::REQUEST_TIMEOUT, "request_expired"));
        }
        Ok(())
    }

    fn validate(&self, registration: bool) -> Result<Vec<TextAnchorInput>, Failure> {
        self.validate_header()?;
        if self.source_dependency.is_some() {
            return Err(failure(StatusCode::UNPROCESSABLE_ENTITY, "invalid_request"));
        }
        if registration && !self.anchors.is_empty()
            || !registration && (self.anchors.is_empty() || self.anchors.len() > 32)
        {
            return Err(failure(StatusCode::UNPROCESSABLE_ENTITY, "invalid_batch"));
        }
        let mut size = 0;
        self.anchors
            .iter()
            .map(|anchor| {
                let input = TextAnchorInput {
                    dependency: decode(&anchor.dependency, 64 * 1024)?,
                    start: decode(&anchor.start, 256)?,
                    end: decode(&anchor.end, 256)?,
                    selected_length: anchor.selected_length,
                };
                size += input.dependency.len() + input.start.len() + input.end.len();
                if size > 256 * 1024 {
                    return Err(failure(StatusCode::UNPROCESSABLE_ENTITY, "batch_too_large"));
                }
                Ok(input)
            })
            .collect()
    }

    fn validate_source(&self) -> Result<Snapshot, Failure> {
        self.validate_header()?;
        if !self.anchors.is_empty() {
            return Err(failure(StatusCode::UNPROCESSABLE_ENTITY, "invalid_batch"));
        }
        let source = self
            .source_dependency
            .as_ref()
            .ok_or_else(|| failure(StatusCode::UNPROCESSABLE_ENTITY, "invalid_request"))?;
        decode_source_dependency(&decode(source, 64 * 1024)?)
            .map_err(|_| failure(StatusCode::UNPROCESSABLE_ENTITY, "invalid_dependency"))
    }
}

impl Server {
    pub fn with_anchor_barriers(mut self, enabled: bool) -> Result<Self> {
        if enabled
            && (self.authenticator.is_none()
                || !self
                    .store
                    .as_ref()
                    .is_some_and(|store| store.supports_durable_checkpoints()))
        {
            return Err(anyhow!(
                "Anchor barriers require server authentication and a supported durable store"
            ));
        }
        self.anchor_barriers_enabled = enabled;
        Ok(self)
    }
}

pub(super) async fn register(
    State(server): State<Arc<Server>>,
    request: Request,
) -> Result<Response, Failure> {
    handle(server, request, Operation::Register).await
}

pub(super) async fn validate(
    State(server): State<Arc<Server>>,
    request: Request,
) -> Result<Response, Failure> {
    handle(server, request, Operation::Anchors).await
}

pub(super) async fn source(
    State(server): State<Arc<Server>>,
    request: Request,
) -> Result<Response, Failure> {
    handle(server, request, Operation::Source).await
}

async fn handle(
    server: Arc<Server>,
    request: Request,
    operation: Operation,
) -> Result<Response, Failure> {
    if !server.anchor_barriers_enabled {
        return Err(failure(StatusCode::NOT_FOUND, "barrier_disabled"));
    }
    // Authenticate before reading or parsing even a bounded request body. This
    // intentionally does not use check_auth's anonymous-development fallback.
    let auth = server
        .authenticator
        .as_ref()
        .ok_or_else(|| failure(StatusCode::UNAUTHORIZED, "server_auth_required"))?;
    let token = request
        .headers()
        .get("authorization")
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.strip_prefix("Bearer "))
        .filter(|v| v.len() <= 8192)
        .ok_or_else(|| failure(StatusCode::UNAUTHORIZED, "server_auth_required"))?;
    auth.verify_server_token(token, current_time_epoch_millis())
        .map_err(|_| failure(StatusCode::UNAUTHORIZED, "server_auth_required"))?;
    if server.cancellation_token.is_cancelled() {
        return Err(failure(StatusCode::SERVICE_UNAVAILABLE, "shutting_down"));
    }
    // Admission includes body collection: slow senders cannot reserve unlimited
    // buffers. There is no unbounded queue behind the four active operations.
    let slot = server
        .anchor_barrier_slots
        .clone()
        .try_acquire_owned()
        .map_err(|_| failure(StatusCode::TOO_MANY_REQUESTS, "barrier_busy"))?;
    let bytes = tokio::time::timeout(
        Duration::from_secs(5),
        to_bytes(request.into_body(), MAX_BODY),
    )
    .await
    .map_err(|_| failure(StatusCode::REQUEST_TIMEOUT, "request_expired"))?
    .map_err(|_| failure(StatusCode::PAYLOAD_TOO_LARGE, "request_too_large"))?;
    let envelope: Envelope = serde_json::from_slice(&bytes)
        .map_err(|_| failure(StatusCode::UNPROCESSABLE_ENTITY, "invalid_request"))?;
    let input = match operation {
        Operation::Register => {
            envelope.validate(true)?;
            Input::Register
        }
        Operation::Anchors => Input::Anchors(envelope.validate(false)?),
        Operation::Source => Input::Source(envelope.validate_source()?),
    };
    let digest = hex::encode(Sha256::digest(&bytes));
    let deadline = envelope.expires_at_ms;
    let owned = server.clone();
    // Dropping this JoinHandle (including HTTP disconnect/timeout) detaches the
    // operation. Its permit, document lock and storage future remain alive in
    // the server tracker. Shutdown drains that tracker before runtime exit.
    let task = server.doc_worker_tracker.spawn(async move {
        let _slot = slot;
        observe(owned, envelope, input, digest).await
    });
    let reply = tokio::time::timeout(
        Duration::from_millis(deadline.saturating_sub(current_time_epoch_millis())),
        task,
    )
    .await
    .map_err(|_| failure(StatusCode::REQUEST_TIMEOUT, "request_expired"))?
    .map_err(|_| failure(StatusCode::SERVICE_UNAVAILABLE, "barrier_failed"))??;
    Ok(([("cache-control", "no-store")], Json(reply)).into_response())
}

async fn observe(
    server: Arc<Server>,
    envelope: Envelope,
    input: Input,
    digest: String,
) -> Result<Value, Failure> {
    let registration = matches!(input, Input::Register);
    let lock = server
        .loading_locks
        .entry(envelope.document_id.clone())
        .or_insert_with(|| Arc::new(AsyncMutex::new(())))
        .clone();
    let _document = lock
        .try_lock_owned()
        .map_err(|_| failure(StatusCode::TOO_MANY_REQUESTS, "document_busy"))?;
    let unavailable = || failure(StatusCode::SERVICE_UNAVAILABLE, "checkpoint_failed");
    let store = server
        .store
        .as_ref()
        .filter(|store| store.supports_durable_checkpoints())
        .ok_or_else(unavailable)?;
    // An unknown managed ID must not initialize content. A false storage result
    // is distinguished from a storage outage, which must never mean "missing".
    if !server.docs.contains_key(&envelope.document_id) {
        if !store
            .exists(&format!("{}/data.ysweet", envelope.document_id))
            .await
            .map_err(|_| unavailable())?
        {
            return Err(failure(StatusCode::NOT_FOUND, "document_unavailable"));
        }
        server
            .load_doc_unlocked(&envelope.document_id, None, None)
            .await
            .map_err(|_| unavailable())?;
    }
    let (awareness, kv) = {
        let doc = server
            .docs
            .get(&envelope.document_id)
            .ok_or_else(unavailable)?;
        // Existing managed notes normally have no routing channel: their
        // inventory/permissions live in the control plane. That authenticated
        // authority supplies the initial ownership binding below. Do not invent
        // a parent channel (and change event routing) just to attach comments.
        // An explicit, conflicting legacy channel remains a hard mismatch.
        if doc
            .get_channel()
            .is_some_and(|channel| channel != envelope.share_id)
        {
            return Err(failure(StatusCode::CONFLICT, "share_mismatch"));
        }
        (doc.awareness(), doc.sync_kv())
    };
    let metadata = kv.get_metadata().unwrap_or_default();
    let saved_generation = metadata.get(GENERATION);
    let saved_share = metadata.get(SHARE);
    let generation = ciborium::value::Value::Text(envelope.generation.clone());
    let share = ciborium::value::Value::Text(envelope.share_id.clone());
    if registration && saved_generation.is_none() && saved_share.is_none() {
        // Only the authenticated control plane may bind the first generation.
        // Never silently rotate a previously bound/restored document.
        kv.update_metadata(GENERATION.into(), generation.clone());
        kv.update_metadata(SHARE.into(), share.clone());
    } else if saved_generation != Some(&generation) || saved_share != Some(&share) {
        return Err(failure(StatusCode::CONFLICT, "generation_mismatch"));
    }
    let observation = match input {
        Input::Register => None,
        Input::Anchors(anchors) => {
            let (placements, snapshot) = {
                let current = awareness.write().map_err(|_| unavailable())?;
                resolve_with_snapshot(&current.doc, &anchors)
                    .map_err(|_| failure(StatusCode::UNPROCESSABLE_ENTITY, "invalid_anchor"))?
            };
            if placements
                .iter()
                .any(|placement| matches!(placement, AnchorPlacement::Invalid))
            {
                return Err(failure(StatusCode::UNPROCESSABLE_ENTITY, "invalid_anchor"));
            }
            if placements
                .iter()
                .any(|placement| matches!(placement, AnchorPlacement::WaitingForSync))
            {
                return Err(failure(StatusCode::CONFLICT, "dependency_not_ready"));
            }
            Some(json!({
                "placements": placements,
                "observed_snapshot_sha256": hex::encode(Sha256::digest(snapshot.encode_v1())),
            }))
        }
        Input::Source(dependency) => {
            let (source, snapshot) = {
                let current = awareness.write().map_err(|_| unavailable())?;
                observe_source(&current.doc, &dependency)
                    .map_err(|_| failure(StatusCode::UNPROCESSABLE_ENTITY, "invalid_dependency"))?
            };
            match source {
                SourceObservation::WaitingForSync => {
                    return Err(failure(StatusCode::CONFLICT, "dependency_not_ready"))
                }
                SourceObservation::DocumentTooLarge => {
                    return Err(failure(
                        StatusCode::PAYLOAD_TOO_LARGE,
                        "source_document_too_large",
                    ))
                }
                SourceObservation::Observed { prefix, complete } => Some(json!({
                    "source_prefix_sha256": hex::encode(Sha256::digest(prefix.as_bytes())),
                    "source_prefix": prefix,
                    "source_complete": complete,
                    "observed_snapshot_sha256": hex::encode(Sha256::digest(snapshot.encode_v1())),
                })),
            }
        }
    };
    // The observation precedes this ordered checkpoint. Edits can proceed while
    // storage writes; a receipt attests to the observed state, not future text.
    kv.persist().await.map_err(|_| unavailable())?;
    if current_time_epoch_millis() >= envelope.expires_at_ms {
        return Err(failure(StatusCode::REQUEST_TIMEOUT, "request_expired"));
    }
    let mut result = json!({
        "protocol_version": 1, "request_id": envelope.request_id,
        "document_id": envelope.document_id, "share_id": envelope.share_id,
        "generation": envelope.generation, "request_sha256": digest,
        "expires_at_ms": envelope.expires_at_ms, "checkpointed_at_ms": current_time_epoch_millis(),
    });
    if let Some(observation) = observation {
        result
            .as_object_mut()
            .unwrap()
            .extend(observation.as_object().unwrap().clone());
    }
    Ok(result)
}

#[cfg(test)]
mod tests;
