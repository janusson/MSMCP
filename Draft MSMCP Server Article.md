# **Designing MSMCP: Bridging Computational Mass Spectrometry and Agentic AI through the Stateless Model Context Protocol**

The integration of high-dimensional scientific instrumentation data into agentic artificial intelligence workflows represents a critical frontier in modern computational chemistry and bioinformatics. Tandem mass spectrometry ($MS^2$) and high-resolution liquid chromatography-mass spectrometry (LC-MS) datasets inherently resist direct ingestion by Large Language Models (LLMs)1. The raw output of a single experimental run can generate millions of data points, rapidly exhausting context windows and triggering catastrophic attention failures during inference. To address this, the Model Context Protocol (MCP) provides a standardized, bidirectional JSON-RPC 2.0 interface that decouples the LLM orchestrator from specialized execution environments, allowing the model to act as a remote orchestrator rather than a brute-force data processor2.  
The development of the Mass Spectrometry Model Context Protocol (MSMCP) server was initiated to bridge the gap between natural language reasoning and the mathematical rigor required to process the "dark metabolome"—the vast majority of mass spectrometry data that remains uncharacterized by traditional library-matching techniques1. This report details the comprehensive architectural decisions, the foundational background research workflows, and the autonomous AI-assisted development processes that culminated in the MSMCP system, exploring how large-scale spectral databases can be seamlessly queried using natural language.

## **The Genesis of Natural Language Database Interrogation**

The conceptualization of MSMCP was driven by a fundamental bottleneck in analytical chemistry: the sheer volume of unstructured $MS^2$ data outpaces human analytical capacity1. Standard conversational language models routinely fail at structural elucidation and unknown analyte identification due to their inability to natively parse mass spectra. When attempting to feed raw mass-to-charge ratio ($m/z$) and intensity arrays directly into a conversational prompt, the system quickly encounters context window overflow. Furthermore, LLMs hallucinate chemical structures when deprived of rigorous deterministic grounding.  
The core architectural requirement was identified as the ability to handle massive spectral databases and execute complex querying via natural language, without forcing the LLM to ingest the underlying matrices. This necessitated the implementation of the "Memory Pointer Pattern," a system design where high-dimensional arrays are cached locally within a secure server state, and the LLM interacts with the data exclusively via pointers and high-level deterministic tools. The LLM performs complex reasoning out-of-context and receives only token-efficient, highly abstracted summaries. This paradigm shift allows the LLM to act as a scientific orchestrator, directing classical algorithms and machine learning models without being crushed by the raw data volume.

## **Background Investigation and Conceptual Mapping**

The initial phase of the project leveraged advanced AI research tools to map the domain constraints. Establishing a server capable of querying large spectral databases with natural language required synthesizing knowledge across two rapidly evolving, disparate fields: computational metabolomics and protocol engineering.

### **Literature Review via Notebook Environments**

The project's foundational research was executed utilizing Gemini Notebook environments to compile an extensive literature review on modern mass spectrometry foundation models1. This investigation revealed that the field is rapidly shifting away from heuristic library matching—such as Compound Discoverer or GNPS workflows, which leave up to 87% of spectra unidentified—toward deep learning spectral foundation models1.  
The deep research phase generated extensive documentation highlighting architectures like the Large Spectral Model for MS2 (LSM-MS2), DreaMS, and Spec2Vec1. These models treat mass spectra not as mathematical artifacts, but as visual or linguistic tokens, enabling self-supervised learning across millions of unannotated spectra1. The research highlighted the specific tokenization strategy required for MS2 data: partitioning integer and decimal components of the $m/z$ value into independent codebooks to preserve parts-per-million (ppm) precision, and scaling relative peak intensities through a dedicated intensity codebook.

| Foundation Architecture | Core Tokenization / Vectorization Mechanism | Downstream Applications |
| :---- | :---- | :---- |
| **LSM-MS2 / LSM1-MS2** | Independent integer/decimal $m/z$ partitioning and intensity codebooks. | Isomer differentiation, de novo generation, property prediction1. |
| **DreaMS** | Self-supervised masked-peak recovery and retention order constraints. | Library matching, property prediction, image co-registration. |
| **MS2DeepScore** | Siamese neural network mapping structural chemical transitions. | Class-specific classification, analog discovery. |
| **Spec2Vec** | Word2Vec embedding of fragment ions and neutral losses. | Fast database pre-filtering, similarity comparisons. |

Furthermore, the research identified the **MSAgent** framework, an autonomous agentic workflow consisting of a Dispatch Agent for planning and a Brain Agent for dynamic tool evaluation. MSAgent demonstrated that linking simulated fragmentation models (e.g., RASSP, NEIMS) with deep learning similarity engines allows an agent to iteratively refine structural rankings, vastly improving exact-match rates over unassisted models. This validated the necessity of building an MCP server to expose similar tools to any compliant LLM client.

### **The Stateless Protocol Paradigm Shift**

Simultaneously, the research phase heavily analyzed the evolving Model Context Protocol specification3. A critical realization emerged from the generated documentation: the MCP ecosystem was undergoing a massive architectural shift with the 2026-07-28 specification release3.  
Historically, early MCP designs relied on a stateful initialize handshake, where capabilities were negotiated and a persistent Mcp-Session-Id was assigned to track the conversational state between the host and the server5. This stateful design fractured when deployed behind enterprise HTTP load balancers, requiring complex sticky-session management or shared Redis instances to route subsequent tool calls to the correct server instance5.  
The 2026-07-28 specification eliminated the transport-level session entirely5. Every JSON-RPC request is now fundamentally independent, carrying its own protocol version, client identity, and capability manifest within the \_meta object5. Furthermore, HTTP-standardized routing headers (Mcp-Protocol-Version, Mcp-Method, Mcp-Name) allow API gateways to route, meter, and cache traffic without performing deep packet inspection on the JSON body5. This fundamental shift dictated that MSMCP must be designed to support horizontal scaling, serverless deployment, and asynchronous task management from its inception5.

## **Project Initiation and Environment Orchestration**

With the theoretical groundwork laid, the transition from research to engineering commenced. The repository was initialized utilizing uv, a highly performant Python package manager and resolver, enforcing strict adherence to Python 3.13+ standards2. The architecture prioritized the FastMCP application framework, a high-level abstraction layer that wraps standard Python functions into validated MCP tools, automatically generating JSON schemas for the host LLM.  
The project dependencies were strictly anchored within the pyproject.toml file to ensure reproducibility2. Critical libraries included mcp\[cli\]\>=2.0.0 for protocol adherence, alongside domain-specific numerical arrays numpy\>=1.26.4 and data validation through pydantic\>=2.12.52. The build backend was configured to utilize hatchling, establishing a modern, standardized packaging pipeline2.  
A crucial environment integration was established by writing a .zed/settings.json file. This configuration instructed the Zed integrated development environment (IDE) to spawn the MSMCP server as a child process using uv run msmcp2. This created a tight, instantaneous feedback loop between the editor's embedded LLM agent and the live MCP context, allowing the agent to test the very server it was building2.

## **Architectural Bootstrapping and Autonomous Code Generation**

The development pipeline was heavily augmented by generative AI. Boilerplate structures, module boundaries, and initial constraints were generated using conversational prompts to establish the foundational architecture. Subsequently, the Zed IDE's integrated agent, powered by the DeepSeek v4 Pro model, was employed for iterative, autonomous code generation and refactoring2.

### **Establishing the Server Boundary and Transport Mechanics**

The entry point of the server, engineered in src/msmcp/server.py, established a critical constraint for MCP servers communicating via standard input/output (stdio) transport2. Because the stdio transport utilizes stdout exclusively for JSON-RPC framing, any rogue print() statement, standard output command, or raw diagnostic logging would fatally corrupt the communication channel, causing the host LLM to instantly lose synchronization with the server2.  
To definitively prevent this, the server was architected with a strict logging boundary. The Python logging.basicConfig was explicitly and exclusively routed to sys.stderr2. The FastMCP instance was instantiated as "MSMCP-MassFlow-Adapter," and a basic diagnostic ping() tool was registered2. This initial tool allowed the host LLM to verify that the underlying computational libraries (such as the mocked massflow dependency) were successfully loaded in the execution environment, returning a simple Pydantic PingResponse containing operational status and availability flags2.

### **Spectral I/O and Context Window Management**

Ingesting raw mass spectrometry files presents an immediate threat to the LLM's context window. The src/msmcp/tools/io.py module handles the parsing of local .mzML and .mgf files, prioritizing token-efficient summarization over raw data dumping2.  
The load\_mzml\_summary tool employs aggressive token optimization strategies to synthesize the experimental data. First, strict scalar quantization is enforced: $m/z$ values are formatted to exactly four decimal places, while retention times (RT) are clamped to two decimal places2. Second, intensity values exceeding $10^6$ are automatically converted to scientific notation, drastically reducing the character density of highly abundant peaks2. Third, the tool accepts a dynamic noise\_threshold parameter, allowing the LLM to autonomously filter out baseline noise and uninformative low-intensity signals before the array is rendered to text2.  
Furthermore, the I/O module implements graceful degradation for unsupported proprietary formats. Vendor formats like Thermo Fisher's .raw and Agilent's .d cannot be parsed directly by open-source tools without specialized DLLs. The MSMCP tool detects these extensions via regular expressions and returns a proactive error message, explicitly instructing the LLM to recommend the use of ProteoWizard's MSConvert utility to the user2. If the required computational libraries are absent from the environment, the tool falls back to a deterministic mock generator, yielding synthetic spectrum data to ensure the agentic workflow can proceed unimpeded during testing and development2.

### **Cheminformatics and Ionization Predictors**

Tandem mass spectrometry relies heavily on understanding the physical chemistry of ionization pathways. The src/msmcp/tools/chem.py module introduces the predict\_adduct\_offset and annotate\_isotopes tools to ground the LLM in exact chemical mathematics, preventing the model from hallucinating molecular masses2.  
The predict\_adduct\_offset tool validates LLM requests against a hardcoded, highly precise database of 14 standard adducts (e.g., \[M+H\]+, \[M+Na\]+, \[M-H\]-)2.

| Canonical Adduct | Polarity | Charge State | Mass Shift Equation | Exact Shift (Δ Da) |
| :---- | :---- | :---- | :---- | :---- |
| \[M+H\]+ | Positive | \+1 | $Proton \- Electron$ | $+1.007276$ |
| \[M+Na\]+ | Positive | \+1 | $Sodium \- Electron$ | $+22.989220$ |
| \[M-H\]- | Negative | \-1 | $-Proton \+ Electron$ | $-1.007276$ |
| \[M+CH3COO\]- | Negative | \-1 | $Acetate \+ Electron$ | $+59.013851$ |

If the LLM generates a non-standard or physically impossible adduct notation, the system rejects the input and returns the canonical list, forcing the LLM to reconsider its ionization reasoning2. The mass shift calculation strictly incorporates the physical constants of the electron rest mass ($0.00054858$ Da) and the proton mass ($1.00727647$ Da), ensuring ppm-level accuracy when translating between neutral mass and observed precursor $m/z$2.  
The annotate\_isotopes tool calculates the theoretical M, M+1, and M+2 isotopologue patterns based on natural isotopic abundances2. The algorithm parses alphanumeric chemical formulas, computing the monoisotopic mass and evaluating the cumulative probability of $+1$ and $+2$ neutron substitutions (e.g., recognizing the substantial $+2$ contributions of $^{34}S$, $^{37}Cl$, and $^{81}Br$)2. It features an intelligent fallback mechanism: if an LLM provides a SMILES string but the RDKit cheminformatics dependency is unavailable, the tool intercepts the ImportError and returns a string instructing the LLM to compute the chemical formula manually and resubmit the request with the boolean flag is\_smiles=False2. This design pattern demonstrates deep resilience, enabling the agentic workflow to self-correct and recover from missing binary dependencies.

### **Similarity Scoring and Precursor Validation**

The src/msmcp/tools/similarity.py module equips the LLM with deterministic mathematical validation, preventing the model from drawing false conclusions based on superficial spectral similarities2. The validate\_precursor tool calculates the mass error between an experimentally observed $m/z$ and a theoretically derived monoisotopic mass using the standard analytical formula:  
$\\Delta ppm \= \\frac{|m\_{theo} \- m\_{exp}|}{m\_{theo}} \\times 10^6$  
If the error exceeds a strict $5.0\\ ppm$ threshold, the tool returns a textual rejection, explicitly stating that the observed spectrum is physically invalid for the hypothesized compound, guiding the agent to discard the hypothesis2.  
The compute\_cosine tool executes a greedy, one-to-one spectral matching algorithm within a defined $m/z$ tolerance2. It bins both the query and reference spectra, aligning intensity vectors and calculating the dot product over the normalized magnitudes2. Crucially, the tool does not simply return an isolated cosine score. Instead, it identifies the most intense query peaks that *failed* to align with the reference spectrum and presents them in a structured Markdown table2. This specific design choice acts as a cognitive mechanism for the LLM, proactively prompting it to deduce structural modifications—such as phosphorylation or methylation—based on the unaligned fragments2.

### **Quality Control and Heuristic Pipeline Routing**

Modern computational mass spectrometry pipelines must frequently decide whether to deploy classical algorithms (like cosine similarity) or advanced machine learning models (like MS2DeepScore or Spec2Vec) depending on the empirical quality of the raw data2. To facilitate this, the src/msmcp/tools/qc.py module introduces the generate\_qc\_summary tool, which analyzes a dataset to guide the LLM's routing logic2.  
The module calculates four key metrics across the dataset:

> * **Signal-to-Noise Ratio (SNR):** Evaluated against synthetic log-normal distributions (typical MS1 SNR $\\approx 50-500$) to identify cohorts with poor signal2.  
> * **Peak Density:** Assessed for excessive sparsity or extreme density, identifying spectra that may lack sufficient diagnostic information or suffer from extreme noise2.  
> * **Chimericity Assessment:** The probability of co-isolation within a specific quadrupole isolation window (e.g., $1.4\\ Da$) is simulated to detect mixed spectra, which heavily degrade classical scoring methods2.  
> * **Diagnostic Fragment Bitmasks:** The presence of biologically significant marker ions is tracked using highly efficient 16-bit integer bitmasks2.

The bitmask implementation provides rapid, low-memory prevalence scanning across thousands of spectra. For example, the presence of the Tyrosine immonium ion at $136.076\\ Da$ occupies bit 0, the Phenylalanine immonium ion at $120.081\\ Da$ occupies bit 1, and generic heuristic markers like the loss of water ($-18.011\\ Da$) occupy higher bits2.  
Based on these extracted metrics, the tool executes a weighted heuristic scoring system, culminating in a definitive routing recommendation to the LLM2.

| Evaluated Dataset Condition | Impact on Pipeline Scoring | Final Recommendation Trigger |
| :---- | :---- | :---- |
| **High Chimericity** (\> 20% co-isolation) | Decreases Classical score, Increases ML score | 🔮 **ML-Based Consensus** (models non-linear mixtures) |
| **High Noise** (\> 20% spectra exhibit SNR \< 3.0) | Decreases Classical score, Increases ML score | 🔮 **ML-Based Consensus** (benefits from ML denoising) |
| **Pure Spectra** (\> 90% isolated cleanly) | Increases Classical score heavily | 🧮 **Classical Cosine Scoring** |
| **Mixed / Marginal Data** | Balanced scoring | ⚖️ **Hybrid Approach** (pre-filter before routing) |

This allows the LLM to act autonomously, dynamically altering its computational path and subsequent tool selection based on the empirical quality of the ingested file, mimicking the decision-making process of a senior analytical chemist2.

## **Overcoming Architectural Bottlenecks: The Async HandleId Pattern**

As the MSMCP architecture expanded to encompass full spectral library searches against SQLite-backed databases, a severe operational bottleneck was identified. Scanning libraries containing thousands of compounds is inherently a CPU-bound, long-running process2. In the context of LLM orchestration, if an agent issues a synchronous request to the search\_library tool, the HTTP connection holds open. If the search takes longer than the host application's timeout window, the connection drops, and the agentic workflow collapses entirely.  
To resolve this limitation, the src/msmcp/tools/search.py module was deeply refactored using the Zed Agent to implement the **Async HandleId Pattern**, a crucial design topology for highly concurrent MCP servers2. This pattern mitigates context timeouts by decoupling the execution dispatcher from the result retrieval mechanism2.

### **The Dispatcher and Poller Mechanism**

The refactoring introduced a module-level state registry, \_JOB\_STORE \= {}, designed to track the lifecycle of background tasks in memory (pending, running, completed, failed)2. The search functionality was subsequently split into two distinct, asynchronous tools:

> 1. **The Dispatcher (search\_library):** When the LLM invokes a library search, the tool generates a short, hex-encoded UUID, logs the job state into \_JOB\_STORE as pending, and uses asyncio.create\_task() to spawn a background coroutine (\_run\_search\_task). The dispatcher immediately returns a lightweight text string to the LLM containing the job\_id and explicit instructions on how to poll for completion (e.g., check\_search\_status(job\_id="a1b2c3d4"))2.  
> 2. **The Poller (check\_search\_status):** The LLM periodically invokes this tool using the provided job\_id. If the task is still processing, the tool returns a status of running, instructing the LLM to wait and poll again. Once completed, the full Markdown report of the search results is delivered into the context window2.

Crucially, the CPU-bound search operations—which include database I/O, greedy cosine matching, and matrix normalizations—are shielded from blocking the main Python asyncio event loop. This is achieved by wrapping the synchronous analytical logic (\_build\_report) in an await asyncio.to\_thread() call, offloading the heavy computation to a separate thread pool2.

### **False Discovery Rates and Small Library Detection**

The background search logic implements chunked iteration, querying the SQLite database in batches of 500 spectra to ensure strict memory safety during extensive scans2. As the engine scores the experimental spectrum against the library, it simultaneously constructs a null distribution by scoring the query against randomized, decoy spectra2.  
However, traditional Target-Decoy False Discovery Rate (FDR) estimation relies heavily on the Law of Large Numbers2. If the spectral library is too small, FDR q-values become statistically unreliable. The MSMCP search tool implements an intelligent, automatic guardrail: upon initialization, it counts the total database records. If the library contains fewer than 2,000 spectra, the tool explicitly overrides the FDR logic, logs a bold scientific warning to the LLM's context, and automatically switches to calculating empirical p-values2.  
If the database is sufficiently large ($\\ge 2000$ spectra), the system calculates the empirical p-values from the null distribution and applies the Benjamini-Hochberg procedure to derive proper q-values2. Ultimately, the tool filters the results, returning only the top hits that satisfy a strict significance threshold ($q \\le 0.05$ or $p \\le 0.05$), completely avoiding the transmission of raw numerical matrices back to the LLM2.

## **Navigating the Stateless Protocol Transformation**

The architectural design patterns implemented throughout MSMCP, particularly the Async HandleId pattern, were heavily influenced by the 2026-07-28 revision of the Model Context Protocol3. The removal of transport-level sessions fundamentally changed how developers must approach state management within AI integrations.  
Because the initialize handshake and the Mcp-Session-Id header have been deprecated, every JSON-RPC request is now isolated5. For MSMCP, this statelessness ensures that the tool definitions (load\_mzml\_summary, compute\_cosine, generate\_qc\_summary) can be served securely by any instance in a load-balanced cluster5. The protocol version, client identity, and capability manifest are transmitted within the \_meta object on every request, while standard HTTP routing headers (Mcp-Method, Mcp-Name) allow enterprise API gateways to route and audit traffic efficiently5.  
Furthermore, the new stateless architecture introduces Multi Round-Trip Requests (MRTR)5. Under previous specifications, if a tool required human intervention or parameter adjustment mid-execution, the server had to hold the connection open indefinitely5. In the 2026-07-28 framework, the tool can simply return an input\_required payload3. The LLM client intercepts this, prompts the human user, and reissues the original tool call with the newly provided parameters5. While MSMCP currently utilizes the asynchronous dispatcher-poller pattern to manage execution delays, future integration of MRTR will allow the search tools to proactively pause execution and ask the user to adjust mass tolerances if zero hits are found, completely avoiding fragile network timeouts5.

## **Synthesis and Future Outlook**

The culmination of the MSMCP project provides a highly robust framework for integrating complex scientific data into generative AI systems. By strictly adhering to the constraints of the stateless Model Context Protocol, executing aggressive token-optimization techniques, and leveraging architectural patterns like Async HandleId, the system allows Large Language Models to interrogate tandem mass spectrometry data safely, efficiently, and accurately2.  
The autonomous, AI-assisted development of the repository highlights a new paradigm in scientific software engineering, where domain experts can rapidly prototype, iterate, and deploy sophisticated infrastructure by harmonizing foundational scientific logic with generative coding models.  
Looking forward, the roadmap for MSMCP involves deeper integration with self-supervised foundation models1. Rather than relying solely on mock cosine similarity or heuristic library matching, subsequent tool modules will execute embedding projections using localized implementations of LSM-MS2, DreaMS, and Spec2Vec1. By generating 1024-dimensional continuous representations of unannotated spectra, the LLM will be able to query the server not just for exact molecular matches, but for analog discovery, subtle isotopic shifts, and broad disease-state clustering1.  
Furthermore, owing to its strict compliance with the stateless 2026-07-28 MCP specification5, the MSMCP server is primed for serverless edge deployment5. This will enable computational chemists to attach lightweight, scalable MSMCP adapters directly to cloud storage buckets containing terabytes of raw LC-MS data. Autonomous LLM agents will soon be able to sift through vast chemical landscapes on demand, dramatically accelerating the discovery and annotation of the dark metabolome.

#### **Works cited**

> 1. MSMCP  
> 2. MCP server configuration.md  
> 3. Stateless MCP: What the 2026-07-28 specification changes for security | Equixly, [https://equixly.com/blog/2026/08/05/stateless-mcp/](https://equixly.com/blog/2026/08/05/stateless-mcp/)  
> 4. MCP is now stateless: what the 2026-07-28 update changes \- Flavio Copes, [https://flaviocopes.com/mcp-2026-07-28-stateless/](https://flaviocopes.com/mcp-2026-07-28-stateless/)  
> 5. MCP 2026-07-28: What's Changing and How to Migrate \- Agentic AI Foundation (AAIF), [https://aaif.io/blog/mcp-2026-07-28-whats-changing-and-how-to-migrate](https://aaif.io/blog/mcp-2026-07-28-whats-changing-and-how-to-migrate)  
> 6. MCP Just Went Stateless — What the 2026 Spec Changes About Scaling on App Service, [https://techcommunity.microsoft.com/blog/appsonazureblog/mcp-just-went-stateless-%E2%80%94-what-the-2026-spec-changes-about-scaling-on-app-servic/4530222](https://techcommunity.microsoft.com/blog/appsonazureblog/mcp-just-went-stateless-%E2%80%94-what-the-2026-spec-changes-about-scaling-on-app-servic/4530222)  
> 7. Model Context Protocol Blog, [https://blog.modelcontextprotocol.io/](https://blog.modelcontextprotocol.io/)  
> 8. MCP Goes Stateless, and Developers Ask Whether That Just Makes It an API Again \- InfoQ, [https://www.infoq.com/news/2026/08/mcp-stateless-gateway/](https://www.infoq.com/news/2026/08/mcp-stateless-gateway/)  
> 9. repomix-output.xml