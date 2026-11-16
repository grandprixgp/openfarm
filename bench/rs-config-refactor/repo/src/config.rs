//! `key = value` settings.

use std::collections::BTreeMap;

/// A line that could not be read.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ConfigError {
    /// The 1-based line number.
    pub line: usize,
    /// What was wrong.
    pub message: String,
}

/// Settings from `key = value` lines; blank lines and `#` comments are skipped.
pub fn load(text: &str) -> Result<BTreeMap<String, String>, ConfigError> {
    let mut settings = BTreeMap::new();
    for (index, raw) in text.lines().enumerate() {
        let line = raw.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let Some((key, value)) = line.split_once('=') else {
            return Err(ConfigError {
                line: index + 1,
                message: "expected key = value".to_string(),
            });
        };
        settings.insert(key.trim().to_string(), value.trim().to_string());
    }
    Ok(settings)
}

/// `base` with each `key = value` line applied over it; unknown keys are refused.
pub fn apply(
    text: &str,
    base: &BTreeMap<String, String>,
) -> Result<BTreeMap<String, String>, ConfigError> {
    let mut merged = base.clone();
    for (index, raw) in text.lines().enumerate() {
        let line = raw.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let Some((key, value)) = line.split_once('=') else {
            return Err(ConfigError {
                line: index + 1,
                message: "expected key = value".to_string(),
            });
        };
        let key = key.trim();
        if !merged.contains_key(key) {
            return Err(ConfigError {
                line: index + 1,
                message: format!("unknown key {key}"),
            });
        }
        merged.insert(key.to_string(), value.trim().to_string());
    }
    Ok(merged)
}
