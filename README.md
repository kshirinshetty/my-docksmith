# Docksmith  
A minimal Docker-like container build and runtime system implemented from scratch.

## Overview  
Docksmith demonstrates how container systems work internally, focusing on layered images, content-addressable storage, deterministic caching, and Linux process isolation. It operates as a single CLI tool with no daemon, storing all state locally.

## Key Concepts  
- Layered image construction using immutable filesystem deltas  
- Content-addressed storage with SHA-256 digests  
- Deterministic build caching with reproducible outputs  
- Process isolation using Linux primitives  

## Build System  
Uses a `Docksmithfile` supporting: `FROM`, `COPY`, `RUN`, `WORKDIR`, `ENV`, `CMD`.  
Only `COPY` and `RUN` create layers; others modify configuration.

## Runtime  
Images are assembled by extracting layers into a temporary root filesystem.  
Processes execute in isolation with no access to the host outside this root.

## Usage 
docksmith build -t name:tag .
docksmith images
docksmith run name:tag
docksmith run -e KEY=value name:tag
docksmith rmi name:tag

## Constraints  
No Docker or external runtimes, no network access during build/run, immutable layers, and fully reproducible builds.
