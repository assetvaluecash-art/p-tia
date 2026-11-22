"""
Minimal Streamlit test to isolate hanging issue.
This is the absolute simplest Streamlit app possible.
"""
import streamlit as st

st.set_page_config(
    page_title="Minimal Test",
    page_icon="🧪",
    layout="wide"
)

st.title("Minimal Streamlit Test")
st.write("If you see this, Streamlit is working!")

st.success("✅ Streamlit loaded successfully")

