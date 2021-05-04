#!/usr/bin/env cwl-runner

cwlVersion: v1.0
class: Workflow

inputs:
  slide: File
  tissue-low-level: int
  tissue-low-label: string
  tissue-high-level: int
  tissue-high-label: string
  tissue-high-filter: string

  gpu: int?

outputs:
  tissue:
    type: Directory
    outputSource: extract-tissue-high/tissue


steps:
  extract-tissue-low:
    run: &extract_tissue
      cwlVersion: v1.0
      class: CommandLineTool
      baseCommand: parallel
      requirements:
        InlineJavascriptRequirement: {}
        DockerRequirement:
          dockerPull: ***REMOVED***:5000/slaid:0.30.3-develop-tissue_model-extract_tissue_eddl_1.1
        InitialWorkDirRequirement:
          listing:
            -  $(inputs.src)
      inputs:
        src:
          type: File
          inputBinding:
            position: 1
        level:
          type: int
          inputBinding:
            prefix: -l
        label:
          type: string
          inputBinding:
            prefix: -f
        filter_slide:
          type: Directory?
          inputBinding:
            prefix: --filter-slide
        filter:
          type: string?
        gpu:
          type: int?
          inputBinding:
            prefix: --gpu
      arguments: ['--overwrite','-o', $(runtime.outdir)]
      outputs:
        tissue:
          type: Directory
          outputBinding:
            glob: '$(inputs.src.basename).zarr'

    in:
      src: slide
      level: tissue-low-level
      label: tissue-low-label
      gpu: gpu
    out: [tissue]

  extract-tissue-high:
    run: *extract_tissue
    in:
      src: slide
      level: tissue-high-level
      label: tissue-high-label
      filter_slide: extract-tissue-low/tissue
      filter: tissue-high-filter
      gpu: gpu
    out: [tissue]


  # classify-tumor:
  #   run: classify-tumor.cwl
  #   in:
  #     src: slide
  #     level: tumor-level
  #     label: tumor-label
  #     filter_slide: extract-tissue/tissue
  #     filter: tumor-filter
  #     gpu: gpu
  #   out:
  #     [tumor]
#
