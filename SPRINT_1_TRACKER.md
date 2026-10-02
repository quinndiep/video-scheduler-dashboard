# 🚀 Media Scheduler - Sprint 1 Tracker

**Duration:** 5 days  
**Goal:** Security & Foundation - Encrypted credentials, persistent jobs, versioned API, FastAPI migration  
**Team:** @quinn-developer (implementation), @code-review (reviews), @planning (coordination)

---

## 📅 Sprint Schedule

### **Day 1: Credential Encryption** 🔐
**Status:** 🟡 Not Started  
**Assigned to:** @quinn-developer  
**Reviewer:** @code-review

#### Tasks:
- [ ] Install `cryptography` and `keyring`
- [ ] Create `security.py` with Fernet encryption helpers
- [ ] Migrate `users.json` to encrypted format (BACKUP FIRST!)
- [ ] Replace index-based YouTube accounts with UUID `account_id`
- [ ] Add `DELETE /api/accounts/{account_id}` endpoint
- [ ] Remove all token logging from console
- [ ] Test encrypted credential load/save

#### Exit Criteria:
- ✅ `users.json` is encrypted
- ✅ Encryption key stored in macOS Keychain
- ✅ No tokens visible in console logs
- ✅ DELETE endpoint works
- ✅ Existing YouTube upload still functional

#### Review Checklist:
- [ ] Fernet encryption implementation secure
- [ ] Encryption key not hardcoded
- [ ] UUID generation correct
- [ ] Backward compatibility for existing users
- [ ] No security vulnerabilities

---

### **Day 2-3: SQLite Job Persistence** 📦
**Status:** 🟡 Not Started  
**Assigned to:** @quinn-developer  
**Reviewer:** @code-review

#### Tasks:
- [ ] Install `sqlalchemy`
- [ ] Create `jobs_db.py` with SQLAlchemy schema
- [ ] Build `JobRepository` class (CRUD operations)
- [ ] Replace in-memory `upload_jobs` dict with DB
- [ ] Implement job resume on server restart
- [ ] Add indexes for performance
- [ ] Test job persistence across restarts

#### Exit Criteria:
- ✅ Jobs stored in SQLite `jobs.db`
- ✅ Jobs survive server restart
- ✅ `JobRepository` thread-safe
- ✅ All job operations use DB (no in-memory dict)
- ✅ Performance acceptable (queries < 100ms)

#### Review Checklist:
- [ ] Schema design appropriate
- [ ] Indexes correct
- [ ] Thread-safety ensured
- [ ] No SQL injection vulnerabilities
- [ ] Job resume logic robust

---

### **Day 4: API Versioning & HTTP Standards** 📋
**Status:** 🟡 Not Started  
**Assigned to:** @quinn-developer  
**Reviewer:** @code-review

#### Tasks:
- [ ] Migrate all routes to `/api/v1/` namespace
- [ ] Keep old routes as redirects (temporary)
- [ ] Standardize error response format
- [ ] Implement proper HTTP status codes (200, 202, 400, 401, 404, 429, 500)
- [ ] Add `Location` header for 202 responses
- [ ] Input validation for all endpoints
- [ ] Test all endpoints return correct status codes

#### Exit Criteria:
- ✅ All routes under `/api/v1/`
- ✅ Consistent error format: `{"success": false, "error": {...}}`
- ✅ Correct HTTP status codes throughout
- ✅ `Location` header on async job creation
- ✅ Input validation returns 400 with details

#### Review Checklist:
- [ ] API versioning consistent
- [ ] Error responses don't leak sensitive info
- [ ] HTTP status codes semantically correct
- [ ] Input validation comprehensive

---

### **Day 5: FastAPI Migration** ⚡
**Status:** 🟡 Not Started  
**Assigned to:** @quinn-developer  
**Reviewer:** @code-review

#### Tasks:
- [ ] Install `fastapi`, `uvicorn`, `pydantic`
- [ ] Create `app.py` with FastAPI
- [ ] Define Pydantic request/response models
- [ ] Port all routes from `server_secure.py`
- [ ] Reuse business logic from existing modules
- [ ] Test under Uvicorn
- [ ] Verify OpenAPI docs at `/docs`
- [ ] Update frontend if needed

#### Exit Criteria:
- ✅ FastAPI server running on port 8770
- ✅ All existing functionality works
- ✅ Auto-generated OpenAPI docs accurate
- ✅ Pydantic validation catches bad inputs
- ✅ No regressions in YouTube upload
- ✅ Performance equal or better than old server

#### Review Checklist:
- [ ] Pydantic models correctly defined
- [ ] Async/await usage correct
- [ ] Business logic properly ported
- [ ] OpenAPI docs accurate
- [ ] Error handling preserved

---

## 📊 Overall Sprint Goals

### **Deliverables:**
1. ✅ Encrypted credential storage (Fernet + macOS Keychain)
2. ✅ Persistent job queue (SQLite + SQLAlchemy)
3. ✅ Versioned API (`/api/v1/`)
4. ✅ FastAPI-based server with auto-docs
5. ✅ No regressions - all existing features work

### **Success Metrics:**
- **Security:** No plain-text credentials, no tokens in logs
- **Reliability:** Jobs survive server restart
- **Standards:** Proper HTTP codes, consistent errors
- **Performance:** API response time < 200ms
- **Quality:** Zero regressions, all tests pass

---

## 🚨 Risk Register

| Risk | Mitigation | Owner |
|------|------------|-------|
| Migration breaks existing users.json | Backup before migration, test with sample data | @quinn-developer |
| SQLite performance issues | Add appropriate indexes, test with 1000+ jobs | @quinn-developer |
| FastAPI breaks frontend | Keep old server running during migration, update incrementally | @quinn-developer |
| Security vulnerability introduced | Code review after each day, penetration testing | @code-review |

---

## 📝 Daily Standup Template

**What I completed:**
- [ ] Task 1
- [ ] Task 2

**What I'm working on next:**
- [ ] Task 3

**Blockers:**
- None / [Description]

**Code review needed:**
- [ ] Files changed: `file1.py`, `file2.py`
- [ ] Ready for @code-review

---

## 📞 Communication Protocol

### **Reporting Progress:**
- End of each day: Message @planning with completed tasks
- When stuck: Message @planning immediately (don't wait)
- Code ready: Message @code-review for review

### **Review Process:**
1. @quinn-developer completes day's tasks
2. @quinn-developer messages @code-review with files changed
3. @code-review reviews within 4 hours
4. If approved ✅: proceed to next day
5. If changes requested ⚠️: fix and re-submit

---

## 🎯 Post-Sprint 1 Plan

After Sprint 1 completion, we proceed to:

**Sprint 2: Task Queue & Platform Integration**
- Day 1-2: Celery + Redis setup
- Day 3: Rate limit & retry logic
- Day 4-5: Instagram API integration

**Sprint 3: More Platforms & Testing**
- Day 1-2: Facebook & TikTok APIs
- Day 3-5: Test suite (pytest, 70%+ coverage)

---

## 📚 Resources

- **FastAPI Docs:** https://fastapi.tiangolo.com/
- **SQLAlchemy Docs:** https://docs.sqlalchemy.org/
- **Cryptography Fernet:** https://cryptography.io/en/latest/fernet/
- **Pydantic:** https://docs.pydantic.dev/

---

## ✅ Sprint Completion Checklist

Before marking Sprint 1 complete:

- [ ] All Day 1-5 tasks completed
- [ ] All code reviewed by @code-review
- [ ] All tests passing
- [ ] No regressions in existing features
- [ ] Documentation updated (README.md)
- [ ] Demo to stakeholder successful

---

**Last Updated:** 2024 (Sprint Start)  
**Next Review:** End of Day 1
