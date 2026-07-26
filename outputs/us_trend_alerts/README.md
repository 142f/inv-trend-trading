# us_trend_alerts

Daily trend alert outputs for US assets.

## Files

```text
us_trend_alerts_YYYYMMDD.md    Human-readable report.
us_trend_alerts_YYYYMMDD.csv   Tabular signal export.
us_trend_alerts_YYYYMMDD.json  Machine-readable signal export.
```

If there are no triggered signals, the JSON may be `[]`, the CSV may contain
only headers, and the Markdown report will state that no signal was triggered.
