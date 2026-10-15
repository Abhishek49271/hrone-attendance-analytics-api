# DECISIONS.md

**1. Which indexes did you create, and why?**

I used a unique index on `emp_code` to prevent duplicate employees. For attendance, I used a compound unique index on `emp_code` and `date`, since an employee should have only one attendance record per day. These indexes also help the API find records without scanning the entire collection. Additional indexes should match the filters and sorting used by the analytics endpoints.

**2. What happens when two identical punch-ins arrive at the same instant?**

Both requests may initially check for an existing record, so checking alone is not enough. The unique attendance index is the final safeguard against duplicate records. One request should create the record successfully and return `201`; the competing request should return `409 Conflict`. The database operation must handle duplicate-key errors properly.

**3. What does the leaderboard return for a tie at the cutoff?**

The leaderboard should use a consistent ordering so results do not change unpredictably between requests. If employees have the same late-arrival value at the cutoff, the implementation should define whether it includes all tied employees or applies a secondary sort. The chosen behaviour should be documented and tested.

**4. How does the department summary count employees with no logs?**

The headcount should come from employees who joined before the end of the reporting period, not just employees found in attendance records. This ensures employees with zero attendance logs are still included in their department’s headcount.

**5. What would you change for 100 times more data?**

I would review query execution plans, add indexes based on actual query patterns, and avoid loading unnecessary records into application memory. I would also test pagination and analytics against a larger dataset and measure performance before making further changes.