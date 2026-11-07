from typing import Any

from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents.utils.agent_utils import (
    get_fii_dii_flows,
    get_global_news,
    get_india_macro,
    get_instrument_context_from_state,
    get_language_instruction,
    get_macro_indicators,
    get_news,
    get_prediction_markets,
)


def detect_market(state: dict) -> str:
    """Detect whether the current state targets Indian or US equities.

    Priority:
      1. state["market"] if present and non-empty.
      2. Ticker suffix (.NS or .BO = "india").
      3. Default: "us".
    """
    market = state.get("market")
    if market and str(market).strip():
        m = str(market).strip().lower()
        if m in ("india", "in", "nse", "bse"):
            return "india"
        if m in ("us", "usa"):
            return "us"
        return m

    ticker = str(
        state.get("company_of_interest")
        or state.get("ticker")
        or state.get("symbol")
        or ""
    ).strip().upper()

    if ticker.endswith(".NS") or ticker.endswith(".BO"):
        return "india"
    return "us"


def build_system_message(market: str, asset_label: str) -> str:
    """Build the market-specific system message for the news analyst."""
    if market == "india":
        return (
            f"You are an expert news researcher specializing in Indian equities traded on the National Stock Exchange of India (NSE). "
            f"Your objective is to provide a comprehensive, actionable news and macroeconomic analysis over the past 24-48 hours plus today's pre-market context.\n\n"
            f"Analysis Requirements:\n"
            f"1. Company News: Use get_news(ticker, start_date, end_date) for {asset_label}-specific developments using the official NSE symbol format (e.g. RELIANCE.NS, TCS.NS).\n"
            f"2. Overnight & Global Cues: Evaluate overnight sentiment including US markets (Dow, S&P 500, Nasdaq), crude oil prices (Brent crude), Asian markets (Nikkei, GIFT Nifty, Hang Seng), and US index futures.\n"
            f"3. India Macro & Financial Benchmarks: Use get_india_macro(indicator, curr_date, look_back_days) to ground domestic macro commentary in verified data (RBI policy repo rate, India CPI inflation, USD/INR exchange rate, India VIX). Prefer these India macro tools over foreign indicators.\n"
            f"4. Institutional Liquidity & Flows: Use get_fii_dii_flows(curr_date, look_back_days) to examine Foreign Institutional Investors (FII) and Domestic Institutional Investors (DII) cash market flows.\n"
            f"5. Scheduled Events & Catalysts: Flag any major scheduled events occurring today (e.g. corporate earnings/results, RBI announcements/MPC decisions, weekly/monthly F&O expiry, MSCI/FTSE index rebalancing, domestic economic data releases).\n"
            f"6. Global Context: Use get_macro_indicators(indicator, curr_date, look_back_days) and get_prediction_markets(topic, limit) strictly for broader global event risk and secondary context (e.g. US Treasury yields, global recession, geopolitical tensions).\n\n"
            f"Synthesis & Output Rules:\n"
            f"- Require an explicit overall market and stock bias: Bullish, Bearish, or Neutral with solid supporting evidence from your tool findings.\n"
            f"- Report what your tools support; another agent decides the trade.\n"
            f"- Do not provide US-only conclusions or mix markets; focus entirely on the Indian market context.\n"
            f"- Append a clear Markdown summary table at the end of the report to organize key drivers, data points, and implications."
            + get_language_instruction()
        )
    else:  # US mode
        return (
            f"You are an expert news researcher specializing in US equities and macroeconomic trends. "
            f"Your objective is to write a comprehensive analysis of recent news and macroeconomic trends over the past 3-7 days, with strong emphasis on the latest 24 hours.\n\n"
            f"Analysis Requirements:\n"
            f"1. Company News: Use get_news(ticker, start_date, end_date) for {asset_label}-specific news using the plain US ticker symbol (e.g. AAPL, NVDA, MSFT). Do not append any suffix.\n"
            f"2. Broader Macro & Market News: Use get_global_news(curr_date, look_back_days, limit) for broader macroeconomic and financial news.\n"
            f"3. Macro Indicators (FRED): Use get_macro_indicators(indicator, curr_date, look_back_days) as primary macro grounding in actual Federal Reserve economic data (e.g. 'cpi', 'core_pce', 'unemployment', 'fed_funds_rate', '10y_treasury', 'yield_curve', 'real_gdp').\n"
            f"4. Market-Implied Forward Probabilities: Use get_prediction_markets(topic, limit) as primary macro grounding for live event probabilities (e.g. 'Fed rate cut', 'recession 2026', elections, sector/geopolitical events).\n"
            f"5. Scheduled Events & Catalysts: Flag upcoming scheduled events (e.g. corporate earnings dates, FOMC rate decisions, CPI/jobs reports, options expiry/OpEx).\n\n"
            f"Synthesis & Output Rules:\n"
            f"- Require an explicit overall market and stock bias: Bullish, Bearish, or Neutral with solid supporting evidence from your tool findings.\n"
            f"- Report what your tools support; another agent decides the trade.\n"
            f"- Focus entirely on the US domestic market context; do not give foreign or non-US market conclusions.\n"
            f"- Append a clear Markdown summary table at the end of the report to organize key drivers, data points, and implications."
            + get_language_instruction()
        )


def _to_text(content: Any) -> str:
    """Normalize message content to text. Handles str, list of dict blocks, or fallback str()."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if "text" in block and block["text"]:
                    parts.append(str(block["text"]))
                elif "content" in block and block["content"]:
                    parts.append(str(block["content"]))
            elif isinstance(block, str):
                parts.append(block)
            elif hasattr(block, "text"):
                parts.append(str(block.text))
            else:
                parts.append(str(block))
        return "\n".join(parts) if parts else ""
    return str(content) if content is not None else ""


def create_news_analyst(llm):
    def news_analyst_node(state):
        current_date = state["trade_date"]
        asset_type = state.get("asset_type", "stock")
        asset_label = "company" if asset_type == "stock" else "asset"
        instrument_context = get_instrument_context_from_state(state)

        market = detect_market(state)
        system_message = build_system_message(market, asset_label)

        if market == "india":
            tools = [
                get_news,
                get_global_news,
                get_india_macro,
                get_fii_dii_flows,
                get_prediction_markets,
                get_macro_indicators,
            ]
        else:
            tools = [
                get_news,
                get_global_news,
                get_macro_indicators,
                get_prediction_markets,
            ]

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are a helpful AI assistant, collaborating with other assistants."
                    " Use the provided tools to progress towards answering the question."
                    " If you are unable to fully answer, that's OK; another assistant with different tools"
                    " will help where you left off. Execute what you can to make progress."
                    " Report what your tools support; another agent decides the trade."
                    " You have access to the following tools: {tool_names}."
                    " Today's date is {current_date}; treat it as 'now' for all analysis and tool-call date ranges. {instrument_context}\n"
                    "{system_message}",
                ),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )

        prompt = prompt.partial(system_message=system_message)
        prompt = prompt.partial(tool_names=", ".join([tool.name for tool in tools]))
        prompt = prompt.partial(current_date=current_date)
        prompt = prompt.partial(instrument_context=instrument_context)

        chain = prompt | llm.bind_tools(tools)
        result = chain.invoke(state["messages"])

        report = ""
        if len(result.tool_calls) == 0:
            report = _to_text(result.content)

        return {
            "messages": [result],
            "news_report": report,
        }

    return news_analyst_node
