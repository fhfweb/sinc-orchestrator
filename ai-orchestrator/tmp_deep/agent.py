from langchain.tools import tool
from langchain.prompts import PromptTemplate
# Isso demonstra uma LCEL Chain com LLM
@tool
def search_db(query: str): pass
chain = PromptTemplate.from_template("...") | ChatOpenAI() | StrOutputParser()