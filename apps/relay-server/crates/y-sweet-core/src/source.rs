//! Bounded source observations for server-derived Bases value dependencies.
//! This module does not authorize access, parse YAML or evaluate formulas.
use crate::anchors::{contains_snapshot, preflight_snapshot};
use anyhow::{bail, Context, Result};
use yrs::updates::decoder::Decode;
use yrs::{Doc, GetString, OffsetKind, ReadTxn, Snapshot, Text, Transact};

pub const MAX_SOURCE_PREFIX_BYTES: usize = 64 * 1024;
pub const MAX_SOURCE_DOCUMENT_UTF16: u32 = 2 * 1024 * 1024;

#[derive(Debug, PartialEq)]
pub enum SourceObservation {
    WaitingForSync,
    DocumentTooLarge,
    Observed { prefix: String, complete: bool },
}

pub fn decode_source_dependency(bytes: &[u8]) -> Result<Snapshot> {
    if bytes.len() > 64 * 1024 {
        bail!("source dependency exceeds protocol limit");
    }
    preflight_snapshot(bytes)?;
    Snapshot::decode_v1(bytes).context("invalid source dependency")
}

/// The prefix and snapshot come from one read transaction. Checking the visible
/// length before get_string bounds its allocation, including a non-ASCII note.
/// The pinned Yrs API has no bounded prefix iterator. The full-note cap is
/// explicit; do not silently inspect a truncated note as if it had no metadata.
pub fn observe_source(doc: &Doc, dependency: &Snapshot) -> Result<(SourceObservation, Snapshot)> {
    if doc.offset_kind() != OffsetKind::Utf16 {
        bail!("source observation requires UTF-16 offsets");
    }
    let text = doc.get_or_insert_text("contents");
    let txn = doc.transact();
    let current = txn.snapshot();
    let observation = if !contains_snapshot(&current, dependency) {
        SourceObservation::WaitingForSync
    } else if text.len(&txn) > MAX_SOURCE_DOCUMENT_UTF16 {
        SourceObservation::DocumentTooLarge
    } else {
        let mut prefix = text.get_string(&txn);
        let complete = prefix.len() <= MAX_SOURCE_PREFIX_BYTES;
        if !complete {
            let mut end = MAX_SOURCE_PREFIX_BYTES;
            while !prefix.is_char_boundary(end) {
                end -= 1;
            }
            prefix.truncate(end);
        }
        SourceObservation::Observed { prefix, complete }
    };
    Ok((observation, current))
}

#[cfg(test)]
mod tests {
    use super::*;
    use yrs::updates::encoder::Encode;
    use yrs::{Options, StateVector, Update};

    fn document() -> Doc {
        Doc::with_options(Options {
            offset_kind: OffsetKind::Utf16,
            ..Options::default()
        })
    }

    #[test]
    fn source_and_snapshot_share_state_and_never_write_content() {
        let doc = document();
        let text = doc.get_or_insert_text("contents");
        text.insert(
            &mut doc.transact_mut(),
            0,
            "---\r\nstatus: Planned 🌿\r\n---\r\nBody",
        );
        let before = doc
            .transact()
            .encode_state_as_update_v1(&StateVector::default());
        let dependency = decode_source_dependency(&doc.transact().snapshot().encode_v1()).unwrap();
        let (result, snapshot) = observe_source(&doc, &dependency).unwrap();
        assert_eq!(
            result,
            SourceObservation::Observed {
                prefix: "---\r\nstatus: Planned 🌿\r\n---\r\nBody".into(),
                complete: true
            }
        );
        assert_eq!(snapshot, dependency);
        assert_eq!(
            before,
            doc.transact()
                .encode_state_as_update_v1(&StateVector::default())
        );
    }

    #[test]
    fn missing_insertions_and_delete_only_updates_are_not_observed_early() {
        let source = document();
        let text = source.get_or_insert_text("contents");
        text.insert(&mut source.transact_mut(), 0, "---\nstatus: Planned\n---\n");
        let dependency = source.transact().snapshot();
        let remote = document();
        assert_eq!(
            observe_source(&remote, &dependency).unwrap().0,
            SourceObservation::WaitingForSync
        );
        let update = source
            .transact()
            .encode_state_as_update_v1(&StateVector::default());
        remote
            .transact_mut()
            .apply_update(Update::decode_v1(&update).unwrap())
            .unwrap();
        text.remove_range(&mut source.transact_mut(), 12, 7);
        let deleted = source.transact().snapshot();
        assert_eq!(deleted.state_map, dependency.state_map);
        assert_eq!(
            observe_source(&remote, &deleted).unwrap().0,
            SourceObservation::WaitingForSync
        );
        let update = source
            .transact()
            .encode_state_as_update_v1(&remote.transact().state_vector());
        remote
            .transact_mut()
            .apply_update(Update::decode_v1(&update).unwrap())
            .unwrap();
        assert!(matches!(
            observe_source(&remote, &deleted).unwrap().0,
            SourceObservation::Observed { .. }
        ));
    }

    #[test]
    fn prefix_is_utf8_safe_and_reports_truncation_and_document_cap() {
        let doc = document();
        let text = doc.get_or_insert_text("contents");
        let source = "a".repeat(MAX_SOURCE_PREFIX_BYTES - 1) + "🌿hidden";
        text.insert(&mut doc.transact_mut(), 0, &source);
        let dependency = doc.transact().snapshot();
        let (result, _) = observe_source(&doc, &dependency).unwrap();
        assert_eq!(
            result,
            SourceObservation::Observed {
                prefix: "a".repeat(MAX_SOURCE_PREFIX_BYTES - 1),
                complete: false
            }
        );
        text.insert(
            &mut doc.transact_mut(),
            0,
            &"a".repeat(MAX_SOURCE_DOCUMENT_UTF16 as usize),
        );
        let dependency = doc.transact().snapshot();
        assert_eq!(
            observe_source(&doc, &dependency).unwrap().0,
            SourceObservation::DocumentTooLarge
        );
    }

    #[test]
    fn malformed_dependency_counts_and_wrong_offset_kind_are_rejected() {
        for bytes in [
            vec![],
            vec![0, 0, 42],
            vec![0, 255, 255, 255, 255, 15],
            vec![0; 65537],
        ] {
            assert!(decode_source_dependency(&bytes).is_err());
        }
        let dependency = decode_source_dependency(&[0, 0]).unwrap();
        assert!(observe_source(&Doc::new(), &dependency).is_err());
        assert_eq!(
            observe_source(&document(), &dependency).unwrap().0,
            SourceObservation::Observed {
                prefix: String::new(),
                complete: true
            }
        );
    }
}
