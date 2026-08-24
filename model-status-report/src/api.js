async function request(path) {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  if (!response.ok) {
    const error = await response.json().catch(() => ({ message: response.statusText }));
    throw new Error(error.message || "请求失败");
  }
  return response.json();
}

export const api = {
  report: (groupId = "") => request(`/api/reports/summary${groupId ? `?group_id=${groupId}` : ""}`),
};
