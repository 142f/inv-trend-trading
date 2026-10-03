from html.parser import HTMLParser

from inv_trend.application.backtest.reporting import build_report_model, render_backtest_html


def test_report_reuses_signal_data_and_preserves_execution_records():
    payload={
        "combinations":[dict(combination_id=name,signal_parameter_hash="same",
                             trades=[{"symbol":"BTC","pnl":1.}],orders=[{"status":"rejected"}])
                        for name in ("A","B")],
        "signal_bundles":[{"signal_parameter_hash":"same","events":[
            {"symbol":"BTC","signal_time":"2020-01-01","direction":"LONG"}]}],
    }
    model=build_report_model(payload)
    assert len(model.charts["signal_series"])==1
    assert len(model.charts["detail_series"])==2
    assert all("signals" not in row for row in model.charts["detail_series"])
    assert model.charts["detail_series"][0]["orders"][0]["status"]=="rejected"


def test_report_is_offline_and_does_not_inject_untrusted_html():
    class Tags(HTMLParser):
        def __init__(self):
            super().__init__()
            self.tags=[]

        def handle_starttag(self,tag,attrs):
            self.tags.append((tag,dict(attrs)))

    payload={"run_id":'</script><img data-audit-injection src=x onerror="alert(1)">',
             "combinations":[],"conclusion":{"summary":"<svg onload=alert(2)>"}}
    document=render_backtest_html(payload)
    parser=Tags()
    parser.feed(document)
    assert not any(tag in {"img","svg"} for tag,_ in parser.tags)
    assert not any(tag=="script" and "src" in attrs for tag,attrs in parser.tags)
    ids={attrs.get("id") for _,attrs in parser.tags}
    assert {"ledgerBody","activeSelect","metricCards","startDate","exportLedger"}<=ids
