# Jamaican Stock Financial Statement Analyzer

A Streamlit app for analysing companies listed on the Jamaica Stock Exchange (JSE). Pick a company and drill into any line item on its Income Statement, Balance Sheet or Cash Flow Statement to see how it changed over time, its growth, its share of the relevant total (the makeup of the company), a plain-language read of what drove the change, the ratios it feeds into, and a simple valuation.

## Run it

    pip install -r requirements.txt

    streamlit run app.py

## Data

Figures are read live from stockanalysis.com via the structured JSON feed behind each financials page. USD-reporting companies are labelled USD and shown in their reported currency.
