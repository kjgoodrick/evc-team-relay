use async_trait::async_trait;
use std::{
    fs::{create_dir_all, read_dir, remove_file, File},
    io::{self, Write},
    path::{Path, PathBuf},
    time::SystemTime,
};
use y_sweet_core::store::{FileInfo, Result, Store, StoreError};

const CHECKPOINT_TEMP_PREFIX: &str = ".relay-checkpoint-";

fn sync_directory(path: &Path) -> io::Result<()> {
    // The relay's Linux/macOS filesystem backend can synchronize directory
    // entries. Other targets must not advertise a durable-anchor capability
    // without a platform-specific implementation and restore tests.
    #[cfg(unix)]
    File::open(path)?.sync_all()?;
    #[cfg(not(unix))]
    let _ = path;
    Ok(())
}

fn parent_directory(path: &Path) -> &Path {
    path.parent()
        .filter(|p| !p.as_os_str().is_empty())
        .unwrap_or(Path::new("."))
}

fn create_directory_tree(path: &Path) -> io::Result<()> {
    let mut missing = Vec::new();
    let mut current = path;
    while !current.exists() {
        missing.push(current.to_path_buf());
        let parent = parent_directory(current);
        if parent == current {
            break;
        }
        current = parent;
    }
    create_dir_all(path)?;
    // Persist newly created ancestor links, not only the eventual file's
    // immediate directory. Existing trees pay no ancestor fsync cost.
    for created in missing.iter().rev() {
        sync_directory(parent_directory(created))?;
    }
    Ok(())
}

fn atomic_write(path: &Path, write: impl FnOnce(&mut File) -> io::Result<()>) -> io::Result<()> {
    let parent = parent_directory(path);
    create_directory_tree(parent)?;
    let mut temporary = tempfile::Builder::new()
        .prefix(CHECKPOINT_TEMP_PREFIX)
        .tempfile_in(parent)?;
    write(temporary.as_file_mut())?;
    temporary.as_file().sync_all()?;
    temporary.persist(path).map_err(|error| error.error)?;
    sync_directory(parent)
}

fn extract_doc_id_from_key(key: &str) -> Result<String> {
    if let Some(parts) = key.strip_prefix("files/") {
        if let Some(doc_id) = parts.split('/').next() {
            return Ok(doc_id.to_string());
        }
    }
    Err(StoreError::NotAuthorized(format!(
        "Invalid key format: {}",
        key
    )))
}

fn extract_hash_from_key(key: &str) -> Result<String> {
    if let Some(parts) = key.strip_prefix("files/") {
        let mut split_parts = parts.split('/');
        split_parts.next(); // Skip doc_id
        if let Some(hash) = split_parts.next() {
            return Ok(hash.to_string());
        }
    }
    Err(StoreError::NotAuthorized(format!(
        "Invalid key format: {}",
        key
    )))
}

pub struct FileSystemStore {
    base_path: PathBuf,
}

impl FileSystemStore {
    pub fn new(base_path: PathBuf) -> std::result::Result<Self, std::io::Error> {
        create_directory_tree(&base_path)?;
        Ok(Self { base_path })
    }
}

#[async_trait]
impl Store for FileSystemStore {
    async fn init(&self) -> Result<()> {
        Ok(())
    }

    async fn get(&self, key: &str) -> Result<Option<Vec<u8>>> {
        let path = self.base_path.join(key);
        let contents = std::fs::read(path);
        match contents {
            Ok(contents) => Ok(Some(contents)),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
            Err(e) => Err(StoreError::ConnectionError(e.to_string())),
        }
    }

    async fn set(&self, key: &str, value: Vec<u8>) -> Result<()> {
        let path = self.base_path.join(key);
        atomic_write(&path, |file| file.write_all(&value)).map_err(|error| {
            StoreError::ConnectionError(format!("Error checkpointing file: {}", error))
        })?;
        Ok(())
    }

    async fn remove(&self, key: &str) -> Result<()> {
        let path = self.base_path.join(key);
        remove_file(path)
            .map_err(|_| StoreError::NotAuthorized("Error removing file.".to_string()))?;
        Ok(())
    }

    async fn exists(&self, key: &str) -> Result<bool> {
        let path = self.base_path.join(key);
        Ok(path.exists())
    }

    async fn list(&self, prefix: &str) -> Result<Vec<FileInfo>> {
        let mut files = Vec::new();
        let mut stack = vec![self.base_path.clone()];

        while let Some(dir) = stack.pop() {
            let entries = match read_dir(&dir) {
                Ok(entries) => entries,
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => continue,
                Err(e) => {
                    return Err(StoreError::ConnectionError(format!(
                        "Failed to read directory: {}",
                        e
                    )));
                }
            };

            for entry in entries.flatten() {
                let path = entry.path();
                if entry
                    .file_name()
                    .to_string_lossy()
                    .starts_with(CHECKPOINT_TEMP_PREFIX)
                {
                    continue;
                }
                if path.is_dir() {
                    stack.push(path);
                    continue;
                }
                if !path.is_file() {
                    continue;
                }

                let key = match path.strip_prefix(&self.base_path) {
                    Ok(rel) => rel.to_string_lossy().into_owned(),
                    Err(_) => continue,
                };
                if !key.starts_with(prefix) {
                    continue;
                }

                let metadata = match path.metadata() {
                    Ok(meta) => meta,
                    Err(_) => continue,
                };
                let last_modified = metadata
                    .modified()
                    .ok()
                    .and_then(|time| time.duration_since(SystemTime::UNIX_EPOCH).ok())
                    .map(|duration| duration.as_millis() as u64)
                    .unwrap_or(0);

                files.push(FileInfo {
                    key,
                    size: metadata.len(),
                    last_modified,
                });
            }
        }

        Ok(files)
    }

    async fn generate_upload_url(
        &self,
        key: &str,
        _content_type: Option<&str>,
        _content_length: Option<u64>,
    ) -> Result<Option<String>> {
        let doc_id = extract_doc_id_from_key(key)?;
        Ok(Some(format!("/f/{}/upload", doc_id)))
    }

    async fn generate_download_url(&self, key: &str) -> Result<Option<String>> {
        let doc_id = extract_doc_id_from_key(key)?;
        let hash = extract_hash_from_key(key)?;
        Ok(Some(format!("/f/{}/download?hash={}", doc_id, hash)))
    }

    fn supports_direct_uploads(&self) -> bool {
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs::File;
    use std::io::Write;
    use tempfile::TempDir;

    #[test]
    fn interrupted_write_keeps_the_previous_complete_snapshot() {
        let directory = TempDir::new().unwrap();
        let path = directory.path().join("doc/data.ysweet");
        atomic_write(&path, |file| file.write_all(b"old complete snapshot")).unwrap();
        let result = atomic_write(&path, |file| {
            file.write_all(b"partial new snapshot")?;
            assert_eq!(std::fs::read(&path).unwrap(), b"old complete snapshot");
            Err(io::Error::other("injected write failure"))
        });
        assert!(result.is_err());
        assert_eq!(std::fs::read(&path).unwrap(), b"old complete snapshot");
        assert_eq!(
            std::fs::read_dir(path.parent().unwrap()).unwrap().count(),
            1
        );
        atomic_write(&path, |file| {
            file.write_all(b"new")?;
            assert_eq!(std::fs::read(&path).unwrap(), b"old complete snapshot");
            file.write_all(b" complete snapshot")
        })
        .unwrap();
        assert_eq!(std::fs::read(&path).unwrap(), b"new complete snapshot");
    }

    #[tokio::test]
    async fn checkpoint_files_reload_and_temporary_files_are_not_listed() {
        let directory = TempDir::new().unwrap();
        let store = FileSystemStore::new(directory.path().join("new/tree")).unwrap();
        store
            .set("doc/data.ysweet", b"snapshot".to_vec())
            .await
            .unwrap();
        let reloaded = FileSystemStore::new(directory.path().join("new/tree")).unwrap();
        assert_eq!(
            reloaded.get("doc/data.ysweet").await.unwrap(),
            Some(b"snapshot".to_vec())
        );
        std::fs::write(
            directory
                .path()
                .join("new/tree/doc/.relay-checkpoint-crash"),
            b"incomplete",
        )
        .unwrap();
        let listed = reloaded.list("doc/").await.unwrap();
        assert_eq!(listed.len(), 1);
        assert_eq!(listed[0].key, "doc/data.ysweet");
    }

    #[tokio::test]
    async fn test_list_files() {
        // Create a temporary directory for testing
        let temp_dir = TempDir::new().unwrap();
        let base_path = temp_dir.path().to_path_buf();

        // Create a store with the temp directory
        let store = FileSystemStore::new(base_path.clone()).unwrap();

        // Create a test directory structure with files
        let doc_id = "test-doc";
        let file_path = base_path.join("files").join(doc_id);
        std::fs::create_dir_all(&file_path).unwrap();

        // Create some test files with different content
        let test_files = vec![
            ("abcdef123456", "test content 1"),
            ("ghijkl789012", "test content 2 with more data"),
            ("mnopqr345678", "small"),
        ];

        for (hash, content) in &test_files {
            let file_path = file_path.join(hash);
            let mut file = File::create(file_path).unwrap();
            file.write_all(content.as_bytes()).unwrap();
        }

        // Test listing files
        let prefix = format!("files/{}", doc_id);
        let files = store.list(&prefix).await.unwrap();

        // Verify that we got the correct number of files
        assert_eq!(files.len(), test_files.len());

        // Verify that all expected files are in the result
        for (hash, content) in &test_files {
            let expected_key = format!("files/{}/{}", doc_id, hash);
            let found = files.iter().any(|file| {
                file.key == expected_key
                    && file.size == content.as_bytes().len() as u64
                    && file.last_modified > 0
            });
            assert!(found, "File with hash {} not found in results", hash);
        }

        // Test listing with a non-existent prefix
        let files = store.list("files/nonexistent").await.unwrap();
        assert_eq!(files.len(), 0);
    }

    #[tokio::test]
    async fn test_list_empty_directory() {
        // Create a temporary directory for testing
        let temp_dir = TempDir::new().unwrap();
        let base_path = temp_dir.path().to_path_buf();

        // Create a store with the temp directory
        let store = FileSystemStore::new(base_path.clone()).unwrap();

        // Create an empty directory
        let doc_id = "empty-doc";
        let file_path = base_path.join("files").join(doc_id);
        std::fs::create_dir_all(&file_path).unwrap();

        // Test listing files in empty directory
        let prefix = format!("files/{}", doc_id);
        let files = store.list(&prefix).await.unwrap();

        // Verify that we got an empty list
        assert_eq!(files.len(), 0);
    }

    #[tokio::test]
    async fn test_generate_upload_url() {
        // Create a temporary directory for testing
        let temp_dir = TempDir::new().unwrap();
        let base_path = temp_dir.path().to_path_buf();

        // Create a store with the temp directory
        let store = FileSystemStore::new(base_path).unwrap();

        // Test generating upload URL - should return relative path for server to process
        let key = "files/test-doc/abcdef1234567890";
        let url = store
            .generate_upload_url(&key, Some("image/png"), Some(1024))
            .await
            .unwrap();

        assert_eq!(url, Some("/f/test-doc/upload".to_string()));
    }

    #[tokio::test]
    async fn test_generate_download_url() {
        // Create a temporary directory for testing
        let temp_dir = TempDir::new().unwrap();
        let base_path = temp_dir.path().to_path_buf();

        // Create a store with the temp directory
        let store = FileSystemStore::new(base_path).unwrap();

        // Test generating download URL - should return relative path for server to process
        let key = "files/test-doc/abcdef1234567890";
        let url = store.generate_download_url(&key).await.unwrap();

        assert_eq!(
            url,
            Some("/f/test-doc/download?hash=abcdef1234567890".to_string())
        );
    }

    #[tokio::test]
    async fn test_extract_doc_id_from_key() {
        let key = "files/test-doc-123/abcdef1234567890";
        let doc_id = extract_doc_id_from_key(key).unwrap();
        assert_eq!(doc_id, "test-doc-123");

        // Test invalid key format
        let invalid_key = "invalid/key";
        assert!(extract_doc_id_from_key(invalid_key).is_err());
    }

    #[tokio::test]
    async fn test_extract_hash_from_key() {
        let key = "files/test-doc/abcdef1234567890";
        let hash = extract_hash_from_key(key).unwrap();
        assert_eq!(hash, "abcdef1234567890");

        // Test invalid key format
        let invalid_key = "files/test-doc"; // Missing hash
        assert!(extract_hash_from_key(invalid_key).is_err());
    }
}
