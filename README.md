# South India Travel Chatbot

Streamlit chatbot on top of a Neo4j graph of South Indian cities and attractions.

## Setup
```bash
pip install -r requirements.txt
cp .env.example .env      # then put your real NEO4J_PASSWORD in .env
streamlit run app.py      # http://localhost:8501
```
Never commit `.env`. Load the data first with `load_data_cypher.txt`.
If your labels/relationships differ, edit the config block at the top of `app.py`.
