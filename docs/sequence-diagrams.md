# Sequence diagrams

Runtime and build interactions, end to end.

## 1. Incremental build

```mermaid
sequenceDiagram
    autonumber
    participant Sch as Scheduler
    participant B as Build runner
    participant Src as Sources
    participant Cache as Model-output cache
    participant M as Model (LLM / embeddings)
    participant PG as Postgres
    participant Idx as Search index

    Sch->>B: start build
    B->>Src: list + fetch sources
    Src-->>B: content + version
    B->>B: compute content hash
    alt hash unchanged
        B->>PG: touch last-seen (no model work)
    else changed
        B->>Cache: look up extraction / embedding
        alt cache hit
            Cache-->>B: stored output (no model call)
        else cache miss
            B->>M: extract / embed
            M-->>B: output
            B->>Cache: persist output durably
        end
        B->>PG: write artifacts + edges
    end
    B->>B: link + rank (graph centrality)
    B->>Idx: upsert changed documents
    B->>PG: run validation gates
    alt gates pass
        B->>PG: activate new version
    else gates fail
        B->>PG: keep last good version active
    end
```

## 2. Knowledge-first retrieval (the common path)

```mermaid
sequenceDiagram
    autonumber
    participant Dev as Developer
    participant Ag as Agent
    participant Br as Context Broker
    participant PG as Postgres

    Dev->>Ag: ask a question / request a change
    Ag->>Br: knowledge search (within budget)
    Br->>Br: authenticate + ACL filter
    Br->>PG: ranked search over active version
    PG-->>Br: candidate artifacts
    Br->>Br: rank (relevance, provenance, centrality), dedupe, cap
    Br->>PG: write retrieval event (audit)
    Br-->>Ag: evidence cards (cited)
    alt knowledge sufficient
        Ag-->>Dev: answer / code with citations
    else gap, stale, or exact code needed
        Ag->>Ag: read specific file (skeleton; exact body on demand)
        Ag-->>Dev: answer / code with citations
    end
```

## 3. Crash-resilient build (no re-paid work)

```mermaid
sequenceDiagram
    autonumber
    participant B as Build runner
    participant M as Model
    participant Cache as Durable output cache
    participant Tx as Build transaction

    B->>M: extract document
    M-->>B: output
    B->>Cache: persist output (committed immediately, on its own connection)
    B->>Tx: stage artifacts (not yet committed)
    Note over B,Tx: build crashes before the final commit
    Tx-->>Tx: rolls back (no partial version is served)
    Note over B,Cache: durable outputs survive the rollback
    B->>Cache: re-run: look up the same output
    Cache-->>B: cached output (zero re-paid model calls)
    B->>Tx: rebuild artifacts, commit, activate
```

## 4. Version activation and serving

```mermaid
sequenceDiagram
    autonumber
    participant B as Build runner
    participant PG as Postgres
    participant Br as Context Broker
    participant Ag as Agent

    B->>PG: validate index/retrieval consistency
    alt consistent
        B->>PG: mark new version active
    else inconsistent
        B->>PG: leave previous version active
    end
    Ag->>Br: request
    Br->>PG: read the single active version
    PG-->>Br: members of the active version only
    Br-->>Ag: results scoped to the active version
```

## 5. Provenance verification

```mermaid
sequenceDiagram
    autonumber
    participant Ag as Agent
    participant Br as Context Broker
    participant PG as Postgres

    Ag->>Br: submit answer + evidence ids used
    Br->>PG: resolve evidence ids (active version, caller-visible)
    PG-->>Br: artifact text + metadata
    Br->>Br: check each quoted claim against the exact source
    alt all claims grounded
        Br-->>Ag: verification receipt (trusted)
    else a claim is unsupported
        Br-->>Ag: rejected (claim not grounded in evidence)
    end
```
