import pandas as pd

from data_agent.tools.lookup import _best_fuzzy_matches, vlookup


def test_vlookup_adds_lookup_fields_and_match_flag() -> None:
    left = pd.DataFrame({"sitecode": ["S001", "S404"]})
    right = pd.DataFrame(
        {
            "sitecode": ["S001"],
            "building_id": ["B001"],
            "building_status": ["active"],
        }
    )

    result = vlookup(
        left,
        right,
        left_key="sitecode",
        right_key="sitecode",
        fields=["building_id", "building_status"],
    )

    assert result.loc[0, "building_id"] == "B001"
    assert result.loc[0, "_lookup_matched"]
    assert not result.loc[1, "_lookup_matched"]


def test_vlookup_supports_normalized_match_and_duplicate_list_strategy() -> None:
    left = pd.DataFrame({"sitecode": [" S001 "]})
    right = pd.DataFrame(
        {
            "sitecode": ["s001", "S001"],
            "building_status": ["active", "inactive"],
        }
    )

    result = vlookup(
        left,
        right,
        left_key="sitecode",
        right_key="sitecode",
        fields=["building_status"],
        match_mode="normalized_exact",
        duplicate_strategy="list",
        aggregate_sep="|",
    )

    assert result.loc[0, "building_status"] == "active|inactive"
    assert result.loc[0, "_lookup_matched"]


def test_vlookup_supports_multi_key_lookup_with_match_explanation() -> None:
    left = pd.DataFrame(
        {
            "sitecode": [" S001 ", "S001", "S002"],
            "owner_code": ["O01", "O99", "O02"],
        }
    )
    right = pd.DataFrame(
        {
            "sitecode": ["s001", "S002"],
            "owner_code": ["o01", "o02"],
            "owner_name": ["Alice", "Bob"],
        }
    )

    result = vlookup(
        left,
        right,
        left_key=["sitecode", "owner_code"],
        right_key=["sitecode", "owner_code"],
        fields=["owner_name"],
        match_mode="normalized_exact",
        confidence_field="_lookup_confidence_owner",
        explanation_field="_lookup_explanation_owner",
    )

    assert result.loc[0, "owner_name"] == "Alice"
    assert result.loc[0, "_lookup_confidence_owner"] == 1.0
    assert result.loc[0, "_lookup_explanation_owner"] == "匹配成功"
    assert not result.loc[1, "_lookup_matched"]
    assert result.loc[1, "_lookup_explanation_owner"] == "匹配失败：未在右表找到对应 key"


def test_vlookup_review_strategy_marks_ambiguous_duplicate_key() -> None:
    left = pd.DataFrame({"owner_code": ["O01", "O02"]})
    right = pd.DataFrame(
        {
            "owner_code": ["O01", "O01", "O02"],
            "owner_name": ["Alice", "Alice Backup", "Bob"],
        }
    )

    result = vlookup(
        left,
        right,
        left_key="owner_code",
        right_key="owner_code",
        fields=["owner_name"],
        duplicate_strategy="review",
        confidence_field="_lookup_confidence_owner",
        explanation_field="_lookup_explanation_owner",
        ambiguity_field="_lookup_ambiguous_owner",
    )

    assert result.loc[0, "owner_name"] == "Alice"
    assert result.loc[0, "_lookup_ambiguous_owner"]
    assert result.loc[0, "_lookup_confidence_owner"] == 0.65
    assert "匹配存在歧义" in result.loc[0, "_lookup_explanation_owner"]
    assert not result.loc[1, "_lookup_ambiguous_owner"]


def test_vlookup_fuzzy_matches_near_duplicate_key_with_confidence() -> None:
    left = pd.DataFrame({"product": ["Macbook Pro 16", "widget-x", "totally-unknown"]})
    right = pd.DataFrame(
        {
            "product": ["macbook pro 16", "widget x"],
            "sku": ["MBP16", "WX"],
        }
    )

    result = vlookup(
        left,
        right,
        left_key="product",
        right_key="product",
        fields=["sku"],
        match_mode="fuzzy",
        fuzzy_threshold=0.8,
        confidence_field="_lookup_confidence_sku",
    )

    # Case-only difference is above threshold -> matched with high confidence.
    assert result.loc[0, "sku"] == "MBP16"
    assert result.loc[0, "_lookup_matched"]
    assert result.loc[0, "_lookup_confidence_sku"] > 0.8
    # "widget-x" vs "widget x" is a single-char swap -> also matches.
    assert result.loc[1, "sku"] == "WX"
    # No candidate is close enough for the third row.
    assert not result.loc[2, "_lookup_matched"]


def _brute_force_fuzzy(left: pd.Series, right: pd.Series, threshold: float) -> pd.DataFrame:
    from difflib import SequenceMatcher

    right_values = [value for value in right.dropna().astype(str).unique() if value]

    def best(value: str) -> tuple[str, float]:
        if not value:
            return "", 0.0
        if value in right_values:
            return value, 1.0
        best_value, best_score = "", 0.0
        for candidate in right_values:
            score = SequenceMatcher(None, value, candidate).ratio()
            if score > best_score:
                best_score, best_value = score, candidate
        if best_score >= threshold:
            return best_value, round(best_score, 4)
        return value, round(best_score, 4)

    pairs = left.astype(str).map(best)
    return pd.DataFrame(
        {
            "match_key": pairs.map(lambda item: item[0]),
            "confidence": pairs.map(lambda item: item[1]),
        },
        index=left.index,
    )


def test_best_fuzzy_matches_equivalent_to_brute_force_with_duplicates() -> None:
    # Repeated left values and blanks exercise the dedup + pruning fast path while
    # staying bit-for-bit identical to the straightforward O(rows x candidates) scan.
    left = pd.Series(["alpha", "alphaa", "alpha", "beta", "", "zzzz", "alphaa"])
    right = pd.Series(["alpha", "beta", "gamma", "", "alpha"])

    for threshold in (0.0, 0.6, 0.8, 0.9, 1.0):
        fast = _best_fuzzy_matches(left, right, threshold).reset_index(drop=True)
        brute = _brute_force_fuzzy(left, right, threshold).reset_index(drop=True)
        assert fast["match_key"].equals(brute["match_key"])
        assert fast["confidence"].equals(brute["confidence"])
