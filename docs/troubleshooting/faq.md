# Frequently Asked Questions

## Cost & Billing

### Q: Why don't my tracked costs match my API provider dashboard?

**A:** This is a known issue, particularly with API aggregators like OpenRouter. MAESTRO calculates costs based on advertised pricing, but actual charges can vary because:

- Aggregators route to different backend providers with varying costs
- Dynamic routing optimizes for speed/availability, not just price  
- Some providers count hidden tokens not reported in their API

Your tracked costs may typically be 40-60% of actual dashboard charges, especially with providers like Openrouter, that may route your calls to further providers with differing rates. This is not a bug in MAESTRO - we calculate correctly based on advertised rates. See [Cost Tracking Discrepancies](common-issues/ai-models.md#cost-tracking-discrepancies) for details and workarounds.

### Q: How can I reduce my API costs?

**A:** Several strategies can help:

1. Use cheaper models for Fast/Mid tiers
2. Reduce research parameters in Settings → Research
3. Use local models for zero API costs
4. Monitor actual dashboard charges, not just tracked costs

## Models & Providers

### Q: Which AI provider should I use?

**A:** It depends on your needs:

- **OpenAI**: Most consistent pricing and reliability
- **OpenRouter**: Access to 100+ models, but pricing can be inconsistent
- **Local Models**: Zero API costs, but requires GPU/CPU resources

### Q: Can I use local LLMs?

**A:** Yes! MAESTRO supports any OpenAI-compatible endpoint. See our [Local LLM Deployment Guide](../deployment/local-llms.md) for setup instructions.

### Q: How do I configure Azure OpenAI?

**A:** Azure OpenAI requires specific URL formatting:

1. Select "Custom Provider"
2. Base URL: `https://your-resource.openai.azure.com/openai/v1/`
   - **Must end with** `/openai/v1/` (not `/openai/deployments/`)
3. Enable "Manual Model Entry" toggle
4. Enter your Azure deployment names (not model names)

**Note**: Different providers may require specific URL path suffixes. Always verify the correct format:
- Azure OpenAI: `/openai/v1/`
- Most others: `/v1/`

See [AI Provider Configuration](../getting-started/configuration/ai-providers.md#azure-openai) for details.

## Common Issues

### Q: Why are my responses slow?

**A:** Check these common causes:

1. Using large/slow models (try faster models like gpt-4o-mini)
2. High context sizes in Research settings
3. Network latency to API provider
4. Rate limiting from provider

### Q: Why do I get "context too large" errors?

**A:** Reduce these settings in Settings → Research:

- `writing_agent_max_context_chars`
- `main_research_doc_results`  
- `main_research_web_results`

## More Help

For detailed troubleshooting, see:
- [AI Model Troubleshooting](common-issues/ai-models.md)
- [Database Issues](common-issues/database.md)
- [Installation Problems](common-issues/installation.md)