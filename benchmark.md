1. Prepare a benchmark comparing reading BAM files to DataFrames (Pandas or Polars) using common
2. python libraries like pysam, oxbow and polars-bio.
The benchmark should load the whole BAM file, extract relevant common fields according to the specification and run count operation on the resulting DataFrame. In the case of polars-bio and oxbow
try to use both eager and lazy/streaming execution modes to compare performance.
3. Use polarsb-bio whl from /tmp/polars_bio-0.20.1-cp39-abi3-macosx_11_0_arm64.whl
4. For the rest use latest versions available on pypi.
5. Use /Users/mwiewior/research/data/WES/NA12878.proper.wes.md.bam for the benchmark.
6. Prepare uv venv and install the required libraries for the benchmark.
7. Each benchmark should be run 2 times as separate process and invocations to eliminate any memory pollution.
8. Use memory_profiler to measure peak memory usage for each benchmark.
9. Prepare a report summarizing the results, including execution time and peak memory usage for each
10. Run it using single-threaded execution to ensure a fair comparison across libraries.
11. Include code snippets for each benchmark in the report, along with instructions on how to reproduce the benchmarks.
12. Summarize the findings, highlighting any significant differences in performance and memory usage between the libraries. Discuss potential reasons for these differences and any implications for users choosing a library for BAM file processing.