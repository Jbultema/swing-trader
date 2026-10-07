# Research basis

The operating hypothesis is intentionally modest: trends can persist over intermediate horizons, but momentum has crash states, trading costs matter, and historical selection can manufacture false discoveries.

## Inputs to the first frozen specification

1. Moskowitz, Ooi, and Pedersen document time-series momentum across 58 liquid futures, with return persistence over roughly one to twelve months. The system borrows the horizon prior, not their short futures implementation. [Paper](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2089463)
2. Daniel and Moskowitz show that momentum can crash after market declines, during high volatility, and in sharp rebounds. The system therefore has market-stress monitoring, exposure caps, and explicit hostile-regime reporting. [Journal article](https://doi.org/10.1016/j.jfineco.2015.12.002)
3. Bollerslev, Hood, Huss, and Pedersen find common structure in volatility and economic value from dynamic risk forecasts. The first version uses a deliberately simple trailing realized-volatility estimate rather than a fragile fitted model. [Review of Financial Studies](https://doi.org/10.1093/rfs/hhy041)
4. Bailey, Borwein, López de Prado, and Zhu formalize the probability of backtest overfitting. The project logs the frozen specification, separates development/validation/holdout intervals, and requires prospective shadow evidence for promotion. [Paper](https://escholarship.org/uc/item/4w1110bb)
5. Barber, Lee, Liu, and Odean find economically large aggregate losses from individual trading, concentrated in aggressive orders. That evidence and SEC warnings are why the first operating candidate is swing-frequency, unlevered, and manually executed—not an intraday bot. [Study](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=529062) and [SEC alert](https://www.sec.gov/newsroom/press-releases/99-114-day-trading-your-dollars-risk-investor-alert)
6. The SEC warns that stop orders can execute materially away from their stop price in fast markets. The dashboard therefore calls its trailing rule a risk trigger, never a guaranteed loss bound. [SEC order-execution rule](https://www.sec.gov/files/rules/final/2024/34-99679.pdf)

## What was not adopted

- No opaque machine-learning return predictor. The available sample is small relative to the strategy search space, and explainability is a core requirement.
- No current-constituent stock backtest. It would embed survivorship bias and make recent technology winners look knowable in advance.
- No same-day strategy. Daily bars cannot validate intraday fills, queue position, spreads, halts, or market impact.
- No short side or derivative proxy for the published trend-following portfolios.
- No optimization against the sealed-holdout score after its first run.

## Falsifiable thesis

After conservative costs, the candidate should reduce maximum drawdown and improve Calmar ratio versus buy-and-hold SPY over the full sample and most hostile regimes. It need not beat SPY CAGR in persistent bull markets. Failure on drawdown control, unstable results across reasonable parameter neighborhoods, or weak prospective shadow performance invalidates promotion.
