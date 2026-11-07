//! Anchor placement primitives for the collaboration feasibility gate.
//!
//! This is not an authorization or durable-sync receipt endpoint. Its caller
//! must check managed document identity/generation/access and establish storage
//! durability. The bounded full-update helper and live batched resolver share
//! the same Yjs/Yrs interpretation and golden vectors.

use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};
use yrs::updates::decoder::Decode;
use yrs::{Doc, OffsetKind, Options, ReadTxn, Snapshot, StickyIndex, Text, Transact, Update};

#[derive(Debug, PartialEq, Serialize)]
#[serde(tag = "status", rename_all = "snake_case")]
pub enum AnchorPlacement {
    Attached { from: u32, to: u32 },
    WaitingForSync,
    Unanchored,
    Invalid,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TextAnchorInput {
    pub dependency: Vec<u8>,
    pub start: Vec<u8>,
    pub end: Vec<u8>,
    pub selected_length: u32,
}

/// Resolve a bounded batch against one existing UTF-16 document/read transaction.
/// Captures dependency state once; never exports or reparses the full document.
/// The caller still owns authorization, generation and checkpoint coordination.
pub fn resolve_on_doc(doc: &Doc, anchors: &[TextAnchorInput]) -> Result<Vec<AnchorPlacement>> {
    Ok(resolve_with_snapshot(doc, anchors)?.0)
}

/// The observation and placements share a transaction. A durable barrier may
/// hash this snapshot after persisting the monotonic state it observed.
pub fn resolve_with_snapshot(
    doc: &Doc,
    anchors: &[TextAnchorInput],
) -> Result<(Vec<AnchorPlacement>, Snapshot)> {
    if doc.offset_kind() != OffsetKind::Utf16 {
        bail!("anchor resolution requires UTF-16 offsets");
    }
    if anchors.is_empty() || anchors.len() > 32 {
        bail!("anchor batch exceeds protocol limit");
    }
    let mut size = 0;
    for anchor in anchors {
        if anchor.dependency.len() > 64 * 1024 || anchor.start.len() > 256 || anchor.end.len() > 256
        {
            bail!("anchor input exceeds protocol limit");
        }
        preflight_snapshot(&anchor.dependency)?;
        size += anchor.dependency.len() + anchor.start.len() + anchor.end.len();
    }
    if size > 256 * 1024 {
        bail!("anchor batch exceeds protocol limit");
    }
    // Empty Y.Text roots have no encoded items; materialize the known root
    // handle without inserting content so an empty-note caret remains valid.
    let text = doc.get_or_insert_text("contents");
    let txn = doc.transact();
    let current = txn.snapshot();
    let placements = anchors
        .iter()
        .map(|anchor| {
            let dependency =
                Snapshot::decode_v1(&anchor.dependency).context("invalid anchor dependency")?;
            let start = StickyIndex::decode_v1(&anchor.start).context("invalid start position")?;
            let end = StickyIndex::decode_v1(&anchor.end).context("invalid end position")?;
            if !contains_snapshot(&current, &dependency) {
                return Ok(AnchorPlacement::WaitingForSync);
            }
            let (Some(from), Some(to)) = (start.get_offset(&txn), end.get_offset(&txn)) else {
                return Ok(AnchorPlacement::Unanchored);
            };
            if !std::ptr::eq(from.branch.as_ref(), text.as_ref())
                || !std::ptr::eq(to.branch.as_ref(), text.as_ref())
                || to.index > text.len(&txn)
            {
                return Ok(AnchorPlacement::Invalid);
            }
            if from.index > to.index || (anchor.selected_length > 0 && from.index == to.index) {
                return Ok(AnchorPlacement::Unanchored);
            }
            Ok(AnchorPlacement::Attached {
                from: from.index,
                to: to.index,
            })
        })
        .collect::<Result<Vec<_>>>()?;
    Ok((placements, current))
}

/// Yrs v1 decoding reserves state-vector/range capacity from counts in the
/// payload. A small malicious payload can otherwise request enormous allocation
/// before truncation is noticed. Check the wire structure without allocating;
/// every declared entry must fit in the remaining bounded byte slice.
pub(crate) fn preflight_snapshot(mut bytes: &[u8]) -> Result<()> {
    fn var(bytes: &mut &[u8], limit: u64) -> Result<u64> {
        let mut value = 0u64;
        for shift in (0..=63).step_by(7) {
            let byte = *bytes.first().context("truncated anchor snapshot")?;
            *bytes = &bytes[1..];
            let part = (byte & 127) as u64;
            if part > u64::MAX >> shift {
                bail!("invalid anchor integer");
            }
            value |= part << shift;
            if value > limit {
                bail!("anchor integer exceeds limit");
            }
            if byte < 128 {
                return Ok(value);
            }
        }
        bail!("invalid anchor integer")
    }
    fn count(bytes: &mut &[u8]) -> Result<usize> {
        let n = var(bytes, u32::MAX as u64)? as usize;
        if n > bytes.len() / 2 {
            bail!("anchor count exceeds payload");
        }
        Ok(n)
    }
    let clients = count(&mut bytes)?;
    for _ in 0..clients {
        var(&mut bytes, (1u64 << 53) - 1)?;
        let ranges = count(&mut bytes)?;
        for _ in 0..ranges {
            let start = var(&mut bytes, u32::MAX as u64)?;
            let len = var(&mut bytes, u32::MAX as u64)?;
            if start + len > u32::MAX as u64 {
                bail!("anchor delete range overflows");
            }
        }
    }
    let states = count(&mut bytes)?;
    for _ in 0..states {
        var(&mut bytes, (1u64 << 53) - 1)?;
        var(&mut bytes, u32::MAX as u64)?;
    }
    if !bytes.is_empty() {
        bail!("trailing anchor snapshot bytes");
    }
    Ok(())
}

/// Include deletion ranges: a delete-only update leaves the state vector unchanged.
pub fn contains_snapshot(current: &Snapshot, dependency: &Snapshot) -> bool {
    if !matches!(
        current.state_map.partial_cmp(&dependency.state_map),
        Some(std::cmp::Ordering::Equal | std::cmp::Ordering::Greater)
    ) {
        return false;
    }
    let mut missing_deletes = dependency.delete_set.clone();
    missing_deletes.diff_with(&current.delete_set);
    missing_deletes.is_empty()
}

pub fn resolve_from_update(
    update: &[u8],
    dependency: &[u8],
    start: &[u8],
    end: &[u8],
    selected_length: u32,
) -> Result<AnchorPlacement> {
    if update.len() > 8 * 1024 * 1024
        || dependency.len() > 64 * 1024
        || start.len() > 256
        || end.len() > 256
    {
        bail!("anchor input exceeds protocol limit");
    }
    // Yrs defaults to UTF-8 bytes. CodeMirror/Yjs use UTF-16 code units.
    // Do not resolve against a default-constructed Doc and return byte offsets.
    let doc = Doc::with_options(Options {
        offset_kind: OffsetKind::Utf16,
        ..Options::default()
    });
    doc.transact_mut()
        .apply_update(Update::decode_v1(update).context("invalid document update")?)?;
    let mut result = resolve_on_doc(
        &doc,
        &[TextAnchorInput {
            dependency: dependency.to_vec(),
            start: start.to_vec(),
            end: end.to_vec(),
            selected_length,
        }],
    )?;
    Ok(result.remove(0))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde::Deserialize;

    #[test]
    fn rejects_snapshot_allocation_bombs_truncation_and_range_overflow() {
        for snapshot in [
            vec![0, 255, 255, 255, 255, 15], // Four billion vector entries in six bytes.
            vec![1, 1, 255, 255, 255, 255, 15, 0], // Four billion deletion ranges.
            vec![1, 1, 1, 255, 255, 255, 255, 15, 1, 0], // Clock + length overflows.
            vec![1, 1, 1],
            vec![0, 0, 42],
        ] {
            assert!(preflight_snapshot(&snapshot).is_err());
        }
        assert!(preflight_snapshot(&[0, 0]).is_ok());
    }

    #[derive(Deserialize)]
    #[serde(rename_all = "camelCase")]
    struct Fixture {
        name: String,
        update: Vec<u8>,
        dependency: Vec<u8>,
        start: Vec<u8>,
        end: Vec<u8>,
        selected_length: u32,
        expected: serde_json::Value,
    }

    #[test]
    fn yjs_generated_anchor_vectors() {
        let value: serde_json::Value =
            serde_json::from_str(include_str!("../tests/fixtures/yjs-text-anchors-v1.json"))
                .unwrap();
        assert_eq!(value["version"], 1);
        let fixtures: Vec<Fixture> = serde_json::from_value(value["fixtures"].clone()).unwrap();
        assert!(fixtures.len() >= 8);
        for f in fixtures {
            let result = resolve_from_update(
                &f.update,
                &f.dependency,
                &f.start,
                &f.end,
                f.selected_length,
            )
            .unwrap_or_else(|e| panic!("{}: {}", f.name, e));
            assert_eq!(
                serde_json::to_value(result).unwrap(),
                f.expected,
                "{}",
                f.name
            );
        }
    }

    #[test]
    fn malformed_or_oversized_inputs_do_not_become_valid_anchors() {
        assert!(resolve_from_update(&[], &[], &[], &[], 0).is_err());
        assert!(resolve_from_update(&[], &[], &vec![0; 257], &[], 0).is_err());
        assert!(resolve_from_update(&[], &vec![0; 65537], &[], &[], 0).is_err());
    }

    #[test]
    fn cached_resolver_refuses_byte_offsets_and_oversized_batches() {
        assert!(resolve_on_doc(&Doc::new(), &[]).is_err());
        let doc = Doc::with_options(Options {
            offset_kind: OffsetKind::Utf16,
            ..Options::default()
        });
        let anchor = TextAnchorInput {
            dependency: vec![],
            start: vec![],
            end: vec![],
            selected_length: 0,
        };
        assert!(resolve_on_doc(&doc, &vec![anchor.clone(); 33]).is_err());
        let oversized = TextAnchorInput {
            dependency: vec![0; 64 * 1024],
            ..anchor
        };
        assert!(resolve_on_doc(&doc, &vec![oversized; 5]).is_err());
    }
}
