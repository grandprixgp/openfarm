//! Whole-number percentages.

/// `part` as a whole percentage of `total`, rounded down.
pub fn percent(part: u64, total: u64) -> u64 {
    part * 100 / total
}
