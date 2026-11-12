---
title: 'About {% data variables.product.prodname_copilot %}'
shortTitle: About
intro: 'Learn how {% data variables.copilot.prodname_copilot_short %} helps you code.'
versions:
  feature: copilot
redirect_from:
  - /copilot/old-path
topics:
  - Copilot
contentType: concepts
---

{% data reusables.copilot.about-copilot %}

{% comment %}
TODO: {% data reusables.copilot.does-not-exist %} this whole block must disappear.
{% endcomment %}

## Supported environments

{% rowheaders %}

| Feature | Supported |
| --- | --- |
| Chat | {% octicon "check" aria-label="Supported" %} |
| Agent | {% octicon "x" aria-label="Not supported" %} |

{% endrowheaders %}

{% ifversion ghes %}
Available on {% data variables.product.prodname_ghe_server %}.
{% else %}
Available on {% data variables.product.prodname_dotcom %}.
{% endif %}

## Getting started

{% data reusables.copilot.steps %}

{% data reusables.copilot.ide-tabs %}

{% codetabs %}
{% codetab python %}
```python
print("hello")
```
{% endcodetab %}
{% codetab javascript %}
```javascript
console.log("hello");
```
{% endcodetab %}
{% endcodetabs %}

{% prompt %}Explain this code{% endprompt %}

{% data reusables.copilot.raw-snippet %}

{% data variables.copilot.unknown_key %} fallback.
