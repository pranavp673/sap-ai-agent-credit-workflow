# SAP AI Agent — Credit Workflow

AI agent integrated with SAP Build Process Automation for automated credit note processing in SAP S/4HANA.

## Architecture

```
S/4HANA → SAP Build Process Automation → SAP AI Core (Agent) → S/4HANA APIs
```

## Repository Structure

```
.pipeline/               # AI Core pipeline and serving configurations
agent/
  serving/               # Model serving templates for AI Core
  src/                   # Agent source code
workflows/               # SAP Build Process Automation artifacts
docker/                  # Dockerfile for AI Core serving
```

## Prerequisites

- SAP BTP subaccount with AI Core and AI Launchpad
- SAP Build Process Automation subscription
- SAP S/4HANA system with OData APIs enabled
- Docker registry access (SAP BTP or Docker Hub)

## Setup

1. Register this repository in SAP AI Core (AI Launchpad → Git Repositories)
2. Create an AI Core Application pointing to `.pipeline/`
3. Build and push the Docker image from `docker/`
4. Create a serving configuration and deployment in AI Core
5. Configure a BTP Destination for the AI Core endpoint
6. Deploy the workflow in SAP Build Process Automation
