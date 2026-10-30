# Keep the shared Ollama server's context size

Vigie runs on the company's internal Ollama server, which also serves about 250 colleagues through OpenWebUI. Ollama reloads a model whenever a request's context size (`num_ctx`) differs from the one the model is loaded with, which would slow down everyone's chat. So Vigie never picks a context size of its own. It reads the size the model is currently loaded with and reuses it, or sends none if the model is not loaded, then truncates item text to fit. On long items the LLM may only see the first part of the text: an accepted trade-off for a POC.
