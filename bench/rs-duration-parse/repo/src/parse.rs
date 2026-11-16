//! Reading sizes and durations from text.

use crate::error::UnitError;

/// Read `"512"`, `"10k"` or `"3m"` as bytes.
pub fn parse_size(text: &str) -> Result<u64, UnitError> {
    let (digits, factor) = match text.chars().last() {
        Some('k') => (&text[..text.len() - 1], 1024),
        Some('m') => (&text[..text.len() - 1], 1024 * 1024),
        _ => (text, 1),
    };
    let number: u64 = digits
        .parse()
        .map_err(|_| UnitError(format!("not a size: {text:?}")))?;
    number
        .checked_mul(factor)
        .ok_or_else(|| UnitError(format!("size too large: {text:?}")))
}
