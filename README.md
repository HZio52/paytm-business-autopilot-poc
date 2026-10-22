# Paytm Business Autopilot POC

A standalone frontend proof of concept for the Paytm Build for India AI Hackathon.

## What is included

- `index.html` - browser entry point
- `paytm-business-autopilot-poc.html` - complete original single-file source
- Synthetic merchant and customer data
- Shared state across Overview, Customers, Messages, Opportunities, AI Teammate, Activity log, Settings, and Profile
- Consent-aware customer segments and simulated WhatsApp delivery statuses
- Controlled offer experiment with a held-out control group
- English, Hindi, and Kannada interface and voice-demo flows
- Profile editing, learned preferences, demo reset, and fast-forward controls

## Current architecture

This hackathon version is a **frontend-only POC**. It does not connect to Paytm production systems, WhatsApp, Soundbox, payment APIs, or a hosted database.

Demo data is synthetic. Demo state is saved in the browser on the device only using `localStorage`. Message delivery, customer replies, voice approval, experiment results, and AI recommendations are simulated in the browser.

## Production-ready next step

A production version would add a secure backend for merchant authentication, Paytm payment and settlement integrations, consent records, customer data protection, message-provider integration, scheduled agent jobs, audit logs, and a real database. Those integrations should not use the synthetic demo credentials or local browser state from this repository.

## Run locally

Open `index.html` in a modern browser, or serve the folder with any static web server.
