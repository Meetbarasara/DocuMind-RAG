from langchain_pinecone import PineconeVectorStore
from langchain_openai import OpenAIEmbeddings
from src.components.config import Config
from src.logger import get_logger
import os

logger = get_logger(__name__)

class RetrievalManager:
    
    def __init__(self,config:Config):
        self.config = config
        self.logger = logger

        if self.config.PINECONE_API_KEY:
            os.environ["PINECONE_API_KEY"] = self.config.PINECONE_API_KEY

        self.vectorstore = PineconeVectorStore(
            index_name=self.config.PINECONE_INDEX_NAME,
            embedding=OpenAIEmbeddings(
                model=self.config.EMBEDDING_MODEL_NAME,
                openai_api_key=self.config.OPENAI_API_KEY,
            ),
            namespace=self.config.PINECONE_NAMESPACE
        )

    def retrieve(self, query: str, filename_filter: str = None, page_filter: str = None):
        """Retrieve relevant docs, filtered by SIMILARITY_THRESHOLD (cosine similarity)."""
        # Cosine similarity threshold: higher score means higher similarity (e.g. 0 to 1)
        similarity_threshold = self.config.SIMILARITY_THRESHOLD
        try:
            if filename_filter:
                filter_dict = {'filename': filename_filter}
                if page_filter:
                    filter_dict['page_number'] = page_filter
                docs_and_scores = self.vectorstore.similarity_search_with_score(
                    query, k=self.config.TOP_K, filter=filter_dict
                )
            else:
                docs_and_scores = self.vectorstore.similarity_search_with_score(
                    query, k=self.config.TOP_K
                )
            # Filter out low-quality results below the similarity threshold
            docs = [doc for doc, score in docs_and_scores if score >= similarity_threshold]
            self.logger.info(f"Retrieved {len(docs)}/{len(docs_and_scores)} docs above threshold")
            return docs
        except Exception as e:
            self.logger.error(f"retrieval failed : {e}")
            return []

    def delete_document_by_filename(self,filename:str):
        try:
            self.vectorstore.delete(filter={"filename": filename})
            self.logger.info(f"Deleted documents with filename {filename}")
        except Exception as e:
            self.logger.error(f"Failed to delete documents with filename {filename}: {e}")