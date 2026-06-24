from setuptools import setup, find_packages
from typing import List

def get_requirements(file_path: str) -> List[str]:
  """
  This function will return the list of requirements
  """
  with open(file_path) as file_obj:
    lines = file_obj.readlines()

    # Nit fix: strip inline "# comment" suffixes and blank lines before
    # handing these to install_requires — it expects clean PEP 508
    # requirement specifiers, not a comment-laden string like
    # "langchain-community  # BM25Retriever for hybrid search (Feature A)",
    # which setuptools can't parse the way `pip install -r` would.
    requirements = []
    for line in lines:
      req = line.split('#', 1)[0].strip()
      if not req or req == '-e .':
        continue
      requirements.append(req)

    return requirements

setup(
    name='Documind',
    version='0.1.0',
    packages=find_packages(),
    install_requires=get_requirements('requirements.txt'),
    description='RAG based document question answering system',
    author='Meet',
)