{#
  A generic test asserting a numeric column stays inside a range.

  dbt_utils.accepted_range does this and is the obvious reach, but pulling in
  dbt_utils means `dbt deps` fetching from the hub during the image build --
  a network call on every rebuild, and a version of someone else's package
  pinned in a lockfile, to get eleven lines of SQL. Not worth the dependency
  here. It would be worth it the moment a second package is needed.

  A generic test is just a query that must return zero rows. Everything dbt
  needs is the `{% test %}` block and a select; the failing rows come back in
  the run results, so `--store-failures` gives you the offending keys rather
  than a bare count.
#}

{% test between(model, column_name, min_value, max_value, inclusive=true) %}

select
    {{ column_name }} as failing_value,
    count()           as rows_affected

from {{ model }}
where {{ column_name }} is not null
  and (
    {% if inclusive %}
        {{ column_name }} < {{ min_value }} or {{ column_name }} > {{ max_value }}
    {% else %}
        {{ column_name }} <= {{ min_value }} or {{ column_name }} >= {{ max_value }}
    {% endif %}
  )
group by failing_value

{% endtest %}
