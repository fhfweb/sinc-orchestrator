
async function performLogin() {
    const res = await axios.get("/api/v1/login");
    console.log(res);
}
