class config:
  """
  centrifugal configuration
  """
   
   #Embedding model and llm model
  EMBEDDING_MODEL_NAME :str = "text-embedding-3-small"
  LLM_MODEL_NAME :str = "gpt-4o-mini"

  #chuking parameters
  CHUNK_SIZE :int = 3000
  NEW_AFTER_N_CHARS: int =2400
  COMBINE_TEXT_UNDER_N_CAHRS :int = 500
  CHUNK_OVERLAP :int = 500

  # Retrieval params
  TOP_K: int = 5
  SIMILARITY_THRESHOLD: float = 0.30

  # Hybrid search (wired up later)
  USE_HYBRID_SEARCH: bool = False

  # OpenAI
  OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY")

  TEMPRATURE :float = 0.7
 