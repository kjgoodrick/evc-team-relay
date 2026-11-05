//! Anchor placement primitives for the collaboration feasibility gate.
//!
//! This is not an authorization or durable-sync receipt endpoint. Its caller
//! must check managed document identity/generation/access and establish storage
//! durability. A bounded full-update reader proves Yjs/Yrs compatibility before
//! integrating a cached, batched resolver into the live relay.

use anyhow::{bail, Context, Result};
use serde::Serialize;
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
    let text = doc.get_or_insert_text("contents");
    doc.transact_mut()
        .apply_update(Update::decode_v1(update).context("invalid document update")?)?;
    let dependency = Snapshot::decode_v1(dependency).context("invalid anchor dependency")?;
    let start = StickyIndex::decode_v1(start).context("invalid start position")?;
    let end = StickyIndex::decode_v1(end).context("invalid end position")?;
    let txn = doc.transact();
    if !contains_snapshot(&txn.snapshot(), &dependency) {
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
    if from.index > to.index || (selected_length > 0 && from.index == to.index) {
        return Ok(AnchorPlacement::Unanchored);
    }
    Ok(AnchorPlacement::Attached {
        from: from.index,
        to: to.index,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde::Deserialize;

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
        let value: serde_json::Value = serde_json::from_str(include_str!(
            "../tests/fixtures/yjs-text-anchors-v1.json"
        ))
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
            assert_eq!(serde_json::to_value(result).unwrap(), f.expected, "{}", f.name);
        }
    }

    #[test]
    fn malformed_or_oversized_inputs_do_not_become_valid_anchors() {
        assert!(resolve_from_update(&[], &[], &[], &[], 0).is_err());
        assert!(resolve_from_update(&[], &[], &vec![0; 257], &[], 0).is_err());
        assert!(resolve_from_update(&[], &vec![0; 65537], &[], &[], 0).is_err());
    }
}
