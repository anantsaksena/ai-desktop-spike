//! CPU capability check for the bundled llama-server.
//!
//! llama-server is compiled with a fixed x86_64 instruction set (listed in
//! scripts/llama_x86_features.txt, embedded here at compile time). On a CPU
//! without one of them it dies with SIGILL ("illegal instruction") the first
//! time ggml runs a kernel, which looks like a random crash to a user. So the
//! app checks before spawning it, and reports which features are missing.
//!
//! aarch64 builds use the baseline ARMv8-A instruction set (NEON is mandatory),
//! so there is nothing to check there.

/// The build's feature list: `<rust feature name> <ggml cmake option>` per line.
const FEATURES_FILE: &str = include_str!("../../scripts/llama_x86_features.txt");

/// Test hook: comma-separated feature names to treat as missing, e.g. "avx2".
/// Lets CI exercise the unsupported-CPU path on any machine. It can only
/// disable the local LLM, never enable anything.
pub const SIMULATE_ENV: &str = "AIDESK_SIMULATE_MISSING_CPU";

#[cfg_attr(not(target_arch = "x86_64"), allow(dead_code))]
pub fn required_features() -> Vec<&'static str> {
    FEATURES_FILE
        .lines()
        .map(str::trim)
        .filter(|l| !l.is_empty() && !l.starts_with('#'))
        .filter_map(|l| l.split_whitespace().next())
        .collect()
}

/// Some(true/false) for features this code knows how to test, None otherwise.
#[cfg(target_arch = "x86_64")]
fn detect(feature: &str) -> Option<bool> {
    // is_x86_feature_detected! needs literals. For AVX-family features it also
    // checks that the OS saves the YMM registers (OSXSAVE/XCR0), not just CPUID.
    Some(match feature {
        "sse4.2" => std::arch::is_x86_feature_detected!("sse4.2"),
        "avx" => std::arch::is_x86_feature_detected!("avx"),
        "avx2" => std::arch::is_x86_feature_detected!("avx2"),
        "bmi2" => std::arch::is_x86_feature_detected!("bmi2"),
        "fma" => std::arch::is_x86_feature_detected!("fma"),
        "f16c" => std::arch::is_x86_feature_detected!("f16c"),
        _ => return None,
    })
}

/// Required features the running CPU lacks. Empty = OK to start llama-server.
pub fn missing_features() -> Vec<String> {
    let simulated: Vec<String> = std::env::var(SIMULATE_ENV)
        .unwrap_or_default()
        .split(',')
        .map(|s| s.trim().to_lowercase())
        .filter(|s| !s.is_empty())
        .collect();

    let mut missing = Vec::new();
    #[cfg(target_arch = "x86_64")]
    for f in required_features() {
        match detect(f) {
            Some(true) => {}
            Some(false) => missing.push(f.to_string()),
            // Unknown name in the features file: fail safe (refuse to start)
            // rather than risk SIGILL. A unit test keeps this from shipping.
            None => missing.push(format!("{f} (unknown to this build)")),
        }
    }
    for s in simulated {
        if !missing.contains(&s) {
            missing.push(s);
        }
    }
    missing
}

/// Message shown to the user. Plain language; the feature names go in the log.
pub fn unsupported_message(missing: &[String]) -> String {
    format!(
        "This computer's processor can't run the built-in AI model: it lacks {}. \
         A CPU from 2013 or later is required (Intel Haswell / AMD Excavator or \
         newer). The rest of the app still works.",
        missing.join(", ").to_uppercase()
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn features_file_parses_and_matches_build_flags() {
        let req = required_features();
        assert_eq!(req, ["sse4.2", "avx", "avx2", "bmi2", "fma", "f16c"]);
        for line in FEATURES_FILE.lines().filter(|l| !l.starts_with('#') && !l.trim().is_empty()) {
            let cols: Vec<_> = line.split_whitespace().collect();
            assert_eq!(cols.len(), 2, "bad line: {line}");
            assert!(cols[1].starts_with("GGML_"), "bad cmake option: {line}");
        }
    }

    #[cfg(target_arch = "x86_64")]
    #[test]
    fn every_listed_feature_is_detectable() {
        for f in required_features() {
            assert!(detect(f).is_some(), "cpu.rs cannot detect '{f}'; add it to detect()");
        }
    }

    #[test]
    fn simulation_reports_missing() {
        // SAFETY: tests in this module don't read the env concurrently.
        unsafe { std::env::set_var(SIMULATE_ENV, "avx2, FMA") };
        let m = missing_features();
        unsafe { std::env::remove_var(SIMULATE_ENV) };
        assert!(m.contains(&"avx2".to_string()) && m.contains(&"fma".to_string()), "{m:?}");
        assert!(unsupported_message(&m).contains("AVX2"));
    }
}
