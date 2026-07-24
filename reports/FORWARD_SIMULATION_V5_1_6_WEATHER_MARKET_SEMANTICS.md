# FORWARD_SIMULATION_V5_1_6_WEATHER_MARKET_SEMANTICS

Weather market parsing is staged:
- Parse weather metric first: highest/high -> high, lowest/low -> low.
- Parse local weather date independently from question text, slug, or ISO date fields.
- Parse temperature bucket into bucket_type, Decimal threshold_value, unit, and canonical_label.
- Extract city only from `temperature in <CITY> be/reach/at <temperature>` style boundaries.
- Parse question text and slug separately, then cross-check city/date/metric/bucket.

Temperature bucket labels:
- exact:30C
- or_below:25C
- or_higher:35C

Conflict rule:
- Any question/slug conflict gives parsing_status=conflict and blocks formal simulated fills.
- Missing city, date, metric, or bucket gives parsing_status=unknown and blocks formal simulated fills.

Regression examples:
- `Will the highest temperature in Beijing be 26°C on July 22?` parses city as `Beijing`.
- `30C`, `20C`, `100F`, and `0C` keep their integer zeroes.
- `exact:25C`, `or_below:25C`, and `or_higher:25C` are distinct buckets.
