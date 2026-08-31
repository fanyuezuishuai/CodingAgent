# include <bits/stdc++.h>
using namespace std;

// true: 水;  false: 土
vector<vector<bool>> field;
vector<vector<bool>> visited;

int n, m;

void dfs(int i, int j){
    // 1. 越界
    if (i < 0 || i >= n || j < 0 || j >= m)
        return;

    // 2. 不是水
    if (field[i][j] == false)
        return;

    // 3. 已经访问
    if (visited[i][j])
        return;

    // 当前格子标记访问
    visited[i][j] = true;

    dfs(i + 1, j);
    dfs(i + 1, j + 1);
    dfs(i + 1, j - 1);

    dfs(i - 1, j);
    dfs(i - 1, j + 1);
    dfs(i - 1, j - 1);

    dfs(i, j + 1);
    dfs(i, j - 1);
}


int main(){
    cin >> n >> m;
    field.resize(n, vector<bool>(m, false));
    visited.resize(n, vector<bool>(m, false));
    for (int i = 0; i < n; i++){
        for (int j = 0; j < m; j++){
            char input;
            cin >> input;
            if (input == 'W'){
                field[i][j] = true;
            }
        }
    }
    int ans = 0;
    for (int i = 0; i < n; i++){
        for (int j = 0; j < m; j++){
            if(field[i][j] == false) continue;
            if(visited[i][j] == false){
                ans++;
                dfs(i, j);
            }
        }
    }
    cout << ans;

    return 0;
}