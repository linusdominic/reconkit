# Fixture taken from data-diff

This directory reproduces the dbt test project from
[datafold/data-diff](https://github.com/datafold/data-diff) (`tests/dbt_artifacts`, at the
archived `master`), so that `reconkit data-diff --dbt` can be checked against the output
data-diff's own test suite expects (`tests/test_dbt.py::test_integration_basic_dbt`).

- `dbt_project.yml`, `target/manifest.json` and `target/run_results.json` are copied as
  they were, except that the original author's home directory in `root_path` and the
  anonymous dbt `user_id` were replaced with `/project` and an all-zero id.
- `build.sql` was generated from `jaffle_shop.duckdb` (schemas `prod` and `dev`; tables
  with their rows, and views) and checked to reproduce every table row for row. It
  replaces the 3 MB DuckDB file, which was written by an older DuckDB release.

data-diff is distributed under the MIT License:

```
Copyright 2022 DataFold Inc.

Permission is hereby granted, free of charge, to any person obtaining a copy of
this software and associated documentation files (the "Software"), to deal in
the Software without restriction, including without limitation the rights to
use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of
the Software, and to permit persons to whom the Software is furnished to do so,
subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS
FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR
COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER
IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
```
