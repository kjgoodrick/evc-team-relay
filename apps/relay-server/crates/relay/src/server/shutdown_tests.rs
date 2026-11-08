use super::*;
use async_trait::async_trait;
use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use y_sweet_core::store::{Result as StoreResult, StoreError};
use yrs::{GetString, Text, Transact};

#[derive(Clone, Default)]
struct FaultStore {
    values: Arc<RwLock<HashMap<String, Vec<u8>>>>,
    fail: Arc<AtomicBool>,
    attempts: Arc<AtomicUsize>,
}

#[async_trait]
impl Store for FaultStore {
    async fn init(&self) -> StoreResult<()> {
        Ok(())
    }
    async fn get(&self, key: &str) -> StoreResult<Option<Vec<u8>>> {
        Ok(self.values.read().unwrap().get(key).cloned())
    }
    async fn set(&self, key: &str, value: Vec<u8>) -> StoreResult<()> {
        self.attempts.fetch_add(1, Ordering::SeqCst);
        if self.fail.load(Ordering::SeqCst) {
            return Err(StoreError::ConnectionError(
                "Disposable write failure".into(),
            ));
        }
        self.values.write().unwrap().insert(key.to_owned(), value);
        Ok(())
    }
    async fn remove(&self, key: &str) -> StoreResult<()> {
        self.values.write().unwrap().remove(key);
        Ok(())
    }
    async fn exists(&self, key: &str) -> StoreResult<bool> {
        Ok(self.values.read().unwrap().contains_key(key))
    }
}

async fn server(store: FaultStore) -> Server {
    Server::new(
        Some(Box::new(store)),
        Duration::from_secs(60),
        None,
        None,
        vec![],
        CancellationToken::new(),
        false,
        None,
    )
    .await
    .unwrap()
}

async fn insert(server: &Server, id: &str, value: &str) {
    let awareness = server.get_or_create_doc(id).await.unwrap().awareness();
    let doc = awareness.read().unwrap();
    let text = doc.doc().get_or_insert_text("body");
    text.insert(&mut doc.doc().transact_mut(), 0, value);
}

async fn restored_text(store: FaultStore, id: &str) -> String {
    let restored = server(store).await;
    let awareness = restored.get_or_create_doc(id).await.unwrap().awareness();
    let value = {
        let doc = awareness.read().unwrap();
        let value = doc
            .doc()
            .get_or_insert_text("body")
            .get_string(&doc.doc().transact());
        value
    };
    restored.drain_workers().await.unwrap();
    value
}

#[tokio::test]
async fn final_checkpoint_failure_prevents_success_and_retains_retryable_dirty_state() {
    let store = FaultStore::default();
    let server = server(store.clone()).await;
    let id = server.create_doc().await.unwrap();
    let second_id = server.create_doc().await.unwrap();
    store.fail.store(true, Ordering::SeqCst);
    insert(&server, &id, "retained final edit").await;
    insert(&server, &second_id, "second retained edit").await;
    let attempts = store.attempts.load(Ordering::SeqCst);
    let error = tokio::time::timeout(Duration::from_secs(2), server.drain_workers())
        .await
        .unwrap()
        .unwrap_err();
    assert!(error.to_string().contains("not a verified backup boundary"));
    assert!(error.to_string().contains("2 documents"));
    assert!(store.attempts.load(Ordering::SeqCst) >= attempts + 2);
    store.fail.store(false, Ordering::SeqCst);
    server.drain_workers().await.unwrap();
    assert_eq!(
        restored_text(store.clone(), &id).await,
        "retained final edit"
    );
    assert_eq!(
        restored_text(store, &second_id).await,
        "second retained edit"
    );
}

#[tokio::test]
async fn final_checkpoint_waits_for_admitted_connection_and_captures_its_last_update() {
    let store = FaultStore::default();
    let server = server(store.clone()).await;
    let id = server.create_doc().await.unwrap();
    let admitted = server.connection_tracker.token();
    // The ordinary persistence loop can finish before an upgraded connection
    // finishes applying the update it had already admitted.
    server.cancellation_token.cancel();
    server.doc_worker_tracker.close();
    server.doc_worker_tracker.wait().await;
    let drain = server.drain_workers();
    tokio::pin!(drain);
    assert!(tokio::time::timeout(Duration::from_millis(20), &mut drain)
        .await
        .is_err());
    insert(&server, &id, "last admitted socket edit").await;
    drop(admitted);
    tokio::time::timeout(Duration::from_secs(2), drain)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(restored_text(store, &id).await, "last admitted socket edit");
}
